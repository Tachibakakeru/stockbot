from flask import Flask, request
from linebot import LineBotApi, WebhookHandler
from linebot.models import (MessageEvent, TextMessage, TextSendMessage, 
                            QuickReply, QuickReplyButton, MessageAction, URIAction,
                            FlexSendMessage)
import requests
import os
import sqlite3
import concurrent.futures
from apscheduler.schedulers.background import BackgroundScheduler
import datetime

app = Flask(__name__)

line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN', 'YOUR_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET', 'YOUR_SECRET'))

session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
try:
    session.get('https://mis.twse.com.tw/stock/index.jsp', timeout=5)
except:
    pass

DB_PATH = '/data/favorites.db' if os.path.exists('/data') else 'favorites.db'

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            favorites TEXT
        )
    ''')
    try:
        c.execute('ALTER TABLE users ADD COLUMN push_enabled INTEGER DEFAULT 0')
    except:
        pass
    
        c.execute('''CREATE TABLE IF NOT EXISTS portfolio (
                     user_id TEXT,
                     code TEXT,
                     cost REAL,
                     shares INTEGER,
                     PRIMARY KEY (user_id, code)
                 )''')
        c.execute('''CREATE TABLE IF NOT EXISTS volatility_log (
                     date TEXT,
                     code TEXT,
                     alert_type TEXT,
                     PRIMARY KEY (date, code, alert_type)
                 )''')
        c.execute('''CREATE TABLE IF NOT EXISTS alerts (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 user_id TEXT,
                 code TEXT,
                 condition TEXT,
                 target_price REAL)''')
    conn.commit()
    conn.close()

init_db()

# ---- DB Functions ----

def update_portfolio(user_id, code, cost, shares):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT cost, shares FROM portfolio WHERE user_id=? AND code=?', (user_id, code))
    row = c.fetchone()
    if row:
        old_cost, old_shares = row
        new_shares = old_shares + shares
        if new_shares <= 0:
            c.execute('DELETE FROM portfolio WHERE user_id=? AND code=?', (user_id, code))
        else:
            new_cost = ((old_cost * old_shares) + (cost * shares)) / new_shares
            c.execute('UPDATE portfolio SET cost=?, shares=? WHERE user_id=? AND code=?', (new_cost, new_shares, user_id, code))
    else:
        if shares > 0:
            c.execute('INSERT INTO portfolio (user_id, code, cost, shares) VALUES (?, ?, ?, ?)', (user_id, code, cost, shares))
    conn.commit()
    conn.close()

def get_portfolio(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT code, cost, shares FROM portfolio WHERE user_id=?', (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def clear_portfolio(user_id, code=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if code:
        c.execute('DELETE FROM portfolio WHERE user_id=? AND code=?', (user_id, code))
    else:
        c.execute('DELETE FROM portfolio WHERE user_id=?', (user_id,))
    conn.commit()
    conn.close()

def get_favorites(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT favorites FROM users WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    if row and row[0]:
        return row[0].split(',')
    return []

def set_favorites(user_id, fav_list):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('REPLACE INTO users (user_id, favorites) VALUES (?, ?)', (user_id, ','.join(fav_list)))
    conn.commit()
    conn.close()

def add_alert(user_id, code, condition, target_price):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO alerts (user_id, code, condition, target_price) VALUES (?, ?, ?, ?)', 
              (user_id, code, condition, target_price))
    conn.commit()
    conn.close()

def get_alerts(user_id=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if user_id:
        c.execute('SELECT id, code, condition, target_price FROM alerts WHERE user_id = ?', (user_id,))
    else:
        c.execute('SELECT id, user_id, code, condition, target_price FROM alerts')
    rows = c.fetchall()
    conn.close()
    return rows

def del_alert(alert_id, user_id=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if user_id:
        c.execute('DELETE FROM alerts WHERE id = ? AND user_id = ?', (alert_id, user_id))
    else:
        c.execute('DELETE FROM alerts WHERE id = ?', (alert_id,))
    conn.commit()
    conn.close()

# ---- API Fetch Functions ----
def get_stock_info(symbol):
    try:
        if not symbol.endswith('.TW'):
            return None
            
        code = symbol[:-3]
        if code.startswith('otc'):
            code = code[3:]
            
        # Cnyes uses TSE01 for TAIEX
        if code == 't00':
            url = "https://ws.api.cnyes.com/ws/api/v1/quote/quotes/TWS:TSE01:INDEX"
        else:
            url = f"https://ws.api.cnyes.com/ws/api/v1/quote/quotes/TWS:{code}:STOCK"
            
        res = session.get(url, timeout=5)
        data = res.json()
        if data.get('statusCode') != 200 or not data.get('data'):
            return None
            
        info = data['data'][0]
        current = float(info.get('6', 0))
        change = float(info.get('11', 0))
        change_pct = float(info.get('56', 0))
        name = info.get('200009', code)
        if code == 't00':
            name = "加權指數"
            
        return {
            'symbol': symbol,
            'name': name,
            'price': current,
            'change': change,
            'change_pct': change_pct,
            'is_us': False
        }
    except Exception:
        return None

def get_us_stock_info(code):
    try:
        url = f'https://hq.sinajs.cn/list=gb_{code.lower()}'
        headers = {'Referer': 'https://finance.sina.com.cn'}
        res = session.get(url, headers=headers, timeout=5)
        text = res.text
        if '=";' in text or '="",' in text or len(text.split('="')) < 2:
            return None
            
        data_str = text.split('="')[1].split('";')[0]
        data = data_str.split(',')
        if len(data) < 5:
            return None
            
        # Ignore Sina's simplified Chinese name (data[0]), use original symbol
        name = code.upper()
        current = float(data[1])
        change_pct = float(data[2])
        change = float(data[4])
        
        return {
            'symbol': code.upper(),
            'name': name,
            'price': current,
            'change': change,
            'change_pct': change_pct,
            'is_us': True
        }
    except Exception:
        return None

def get_crypto_info(symbol):
    try:
        query_symbol = f'{symbol.upper()}USDT'
        if symbol.upper() == 'USDT':
            query_symbol = 'USDTUSD'
            
        url = f'https://api.binance.com/api/v3/ticker/24hr?symbol={query_symbol}'
        res = session.get(url, timeout=5).json()
        if 'lastPrice' not in res:
            return None
            
        current = float(res['lastPrice'])
        change = float(res['priceChange'])
        change_pct = float(res['priceChangePercent'])
        
        return {
            'symbol': symbol.upper(),
            'name': symbol.upper() + ' (Crypto)',
            'price': current,
            'change': change,
            'change_pct': change_pct,
            'is_us': True,
            'is_crypto': True
        }
    except Exception:
        return None

def get_futures_info(commodity_id):
    """Query TAIFEX futures real-time data, works during night session (夜盤 15:00-05:00)"""
    try:
        url = 'https://mis.taifex.com.tw/futures/api/getQuoteList'
        payload = {"MarketType": "0", "CommodityID": commodity_id.upper(),
                   "ContractMonth": "", "RowSize": "5", "PageNo": "",
                   "SortColumn": "", "AscDesc": "A"}
        resp = session.post(url, json=payload, timeout=5)
        data = resp.json()
        if data.get('RtCode') != '0':
            return None
        quote_list = data.get('RtData', {}).get('QuoteList', [])
        if not quote_list:
            return None
        # Skip aggregate row (CTotalVolume empty), use first real contract
        item = next((q for q in quote_list if q.get('CTotalVolume')), quote_list[0])
        last = item.get('CLastPrice', '')
        ref = item.get('CRefPrice', '')
        if not last or not ref:
            return None
        last, ref = float(last), float(ref)
        change = last - ref
        change_pct = (change / ref * 100) if ref != 0 else 0
        disp = item.get('DispEName', commodity_id.upper())
        name_base = {'TXF': '台指期', 'MTX': '小台指', 'TE': '電子期',
                     'TF': '金融期', 'XIF': '非金電期'}.get(commodity_id.upper(), commodity_id.upper())
        return {
            'symbol': commodity_id.upper(),
            'name': f'{name_base} ({disp})',
            'price': last,
            'change': change,
            'change_pct': change_pct,
            'is_futures': True,
            'is_us': False,
        }
    except Exception as e:
        print(f"Futures error: {e}")
        return None

def update_stock_names_db():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('CREATE TABLE IF NOT EXISTS stock_names (code TEXT PRIMARY KEY, name TEXT)')
        
        try:
            res = requests.get('https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL', timeout=10).json()
            for item in res:
                c.execute('INSERT OR REPLACE INTO stock_names (code, name) VALUES (?, ?)', (item['Code'], item['Name']))
        except Exception as e:
            print("TWSE names error:", e)
            
        try:
            res = requests.get('https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes', timeout=10).json()
            for item in res:
                c.execute('INSERT OR REPLACE INTO stock_names (code, name) VALUES (?, ?)', (item['SecuritiesCompanyCode'], item['CompanyName']))
        except Exception as e:
            print("TPEx names error:", e)
            
        conn.commit()
        conn.close()
        print("Stock names DB updated.")
    except Exception as e:
        print("update_stock_names_db error:", e)

import threading
threading.Thread(target=update_stock_names_db).start()

def resolve_stock_code(arg):
    if not arg:
        return arg
    import re
    if re.match(r'^[A-Za-z0-9.]+$', arg):
        return arg.upper()
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT code FROM stock_names WHERE name=? OR name=?", (arg, arg.upper()))
        row = c.fetchone()
        conn.close()
        if row:
            return row[0]
    except:
        pass
    return arg

def fetch_data(code):
    # Futures check before stock name resolution
    _futures_map = {'TXF': 'TXF', 'TX': 'TXF', 'MTX': 'MTX', 'TE': 'TE', 'TF': 'TF', 'XIF': 'XIF'}
    if code.upper() in _futures_map:
        return get_futures_info(_futures_map[code.upper()])

    code = resolve_stock_code(code)
    indices_map = {
        '大盤': 't00', '加權指數': 't00', '台指': 't00',
        '道瓊': 'dji', 'DOW': 'dji',
        '那斯達克': 'ixic', '納斯達克': 'ixic', 'NASDAQ': 'ixic',
        '標普': 'inx', 'S&P': 'inx', 'SP500': 'inx',
        '費半': 'sox', 'SOX': 'sox',
        'UNITY': 'U', 'UNITYSOFTWARE': 'U'
    }
    crypto_aliases = {
        '比特幣': 'BTC', '以太幣': 'ETH', '狗狗幣': 'DOGE', '瑞波幣': 'XRP', '萊特幣': 'LTC', 'SOLANA': 'SOL'
    }
    popular_cryptos = {'BTC', 'ETH', 'BNB', 'SOL', 'XRP', 'DOGE', 'ADA', 'TRX', 'AVAX', 'DOT', 
                       'LINK', 'TON', 'SHIB', 'BCH', 'NEAR', 'UNI', 'LTC', 'PEPE', 'APT', 'SUI', 'FET',
                       'USDT', 'USDC', 'DAI', 'FDUSD', 'TUSD', 'U'}
    
    # 1. Check Crypto Aliases / Popular
    target_crypto = None
    if code.upper() in crypto_aliases:
        target_crypto = crypto_aliases[code.upper()]
    elif code in crypto_aliases:
        target_crypto = crypto_aliases[code]
    elif code.upper() in popular_cryptos:
        target_crypto = code.upper()
        
    if target_crypto:
        data = get_crypto_info(target_crypto)
        if data:
            if code.upper() != target_crypto:
                data['name'] = f"{code} ({data['symbol']})"
            return data
        
    # 2. Check Indices map
    if code.upper() in indices_map:
        mapped_code = indices_map[code.upper()]
    elif code in indices_map:
        mapped_code = indices_map[code]
    else:
        mapped_code = code

    # 3. TWSE TSE query
    if mapped_code == 't00':
        data = get_stock_info('t00.TW')
    else:
        query_code_tse = mapped_code + '.TW' if '.' not in mapped_code else mapped_code
        data = get_stock_info(query_code_tse)
        
        if not data:
            # Try TWSE OTC query
            if '.' not in mapped_code and not mapped_code.startswith('otc'):
                query_code_otc = 'otc' + mapped_code + '.TW'
                data = get_stock_info(query_code_otc)
                
    # 4. Sina HQ fallback (US stocks or indices)
    if not data:
        if mapped_code.isalpha() or '.' in mapped_code:
            data = get_us_stock_info(mapped_code.split('.')[0])
            if data and code != mapped_code:
                data['name'] = f"{code} ({data['symbol']})"
                
    # 5. Generic Crypto Fallback (Any altcoin on Binance)
    if not data:
        data = get_crypto_info(code)
            
    return data

def fetch_news(code):
    try:
        url = f"https://api.cnyes.com/media/api/v1/search?q={code}&limit=5"
        res = requests.get(url, timeout=5)
        data = res.json()
        news_str_list = []
        if 'items' in data and 'data' in data['items']:
            for item in data['items']['data'][:5]:
                pub_time = datetime.datetime.fromtimestamp(item.get('publishAt', 0)).strftime('%Y-%m-%d %H:%M')
                title = item.get('title', '')
                link = f"https://news.cnyes.com/news/id/{item.get('newsId')}"
                news_str_list.append(f"📰 {title}\n🕒 {pub_time}\n🔗 {link}")
        if not news_str_list:
            return "目前沒有最新新聞。"
        return "\n\n".join(news_str_list)
    except Exception:
        return "⚠️ 無法取得最新新聞"

def get_institutional(code):
    try:
        stock_data = fetch_data(code)
        if not stock_data:
            return f"⚠️ 查無 {code}，請確認代碼是否正確。"
        
        if stock_data.get('is_crypto'):
            return f"⚠️ 【{stock_data['name']}】為加密貨幣，無三大法人買賣超資料。"
        if stock_data.get('is_us'):
            return f"⚠️ 【{stock_data['name']}】為美股，無台灣三大法人買賣超資料。"
            
        real_code = stock_data['symbol'].replace('.TW', '')
        is_otc = False
        if real_code.startswith('otc'):
            real_code = real_code.replace('otc', '')
            is_otc = True
            
        if real_code == 't00':
            return "⚠️ 大盤指數無個別法人買賣超資料。"
            
        name = stock_data['name']
        
        if is_otc:
            url = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php?l=zh-tw&o=json"
            res = requests.get(url, timeout=5).json()
            for row in res.get('tables', [{}])[0].get('data', []):
                if row[0] == real_code:
                    foreign = int(row[10].replace(',',''))
                    trust = int(row[13].replace(',',''))
                    dealer = int(row[22].replace(',',''))
                    total = int(row[23].replace(',',''))
                    return f"🕵️ 【{real_code} {name}】三大法人買賣超 (張)\n\n外資：{foreign//1000:,}\n投信：{trust//1000:,}\n自營商：{dealer//1000:,}\n──────────\n合計：{total//1000:,}"
        else:
            url = "https://www.twse.com.tw/fund/T86?response=json&selectType=ALL"
            res = requests.get(url, timeout=5).json()
            for row in res.get('data', []):
                if row[0] == real_code:
                    foreign = int(row[4].replace(',',''))
                    trust = int(row[10].replace(',',''))
                    dealer = int(row[11].replace(',',''))
                    total = int(row[18].replace(',',''))
                    return f"🕵️ 【{real_code} {name}】三大法人買賣超 (張)\n\n外資：{foreign//1000:,}\n投信：{trust//1000:,}\n自營商：{dealer//1000:,}\n──────────\n合計：{total//1000:,}"
                    
        return f"⚠️ 查無 {real_code} 今日的三大法人買賣超，可能是今天無交易或尚未更新。"
    except Exception as e:
        print(f"Inst Error: {e}")
        return "⚠️ 獲取資料失敗"

def get_hot_stocks_msg():
    try:
        url = "https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX20"
        res = requests.get(url, timeout=5).json()
        msg = "🔥 【台股成交量熱門前五名】\n\n"
        for i, item in enumerate(res[:5]):
            change = item.get('Dir', '') + item.get('Change', '')
            msg += f"第{i+1}名: {item['Name']} ({item['Code']})\n💰 價: {item['ClosingPrice']} ({change})\n📊 量: {int(item['TradeVolume'])//1000:,} 張\n\n"
        return msg.strip()
    except Exception:
        return "⚠️ 獲取熱門股失敗"

def get_dividend_info(code):
    try:
        from bs4 import BeautifulSoup
        import re
        
        stock_data = fetch_data(code)
        if not stock_data:
            return f"⚠️ 查無 {code} 的基本面資料，請確認代碼是否正確。"
            
        if stock_data.get('is_crypto'):
            return f"⚠️ 【{stock_data['name']}】為加密貨幣，無傳統股利政策。"
            
        real_code = stock_data['symbol'].replace('.TW', '')
        if real_code.startswith('otc'):
            real_code = real_code.replace('otc', '')
        elif real_code == 't00':
            return "⚠️ 大盤指數無配息資料。"
            
        pe_info = ""
        try:
            url_twse = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
            res_twse = requests.get(url_twse, timeout=3).json()
            if isinstance(res_twse, list):
                for item in res_twse:
                    if item.get('Code') == real_code:
                        pe_info = f"本益比 (P/E)：{item['PEratio']} | 股價淨值比 (P/B)：{item['PBratio']}\n\n"
                        break
        except Exception:
            pass
            
        url = f"https://tw.stock.yahoo.com/quote/{real_code}/dividend"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        res = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(res.text, 'html.parser')
        
        name = stock_data['name']
        msg = f"💰 【{real_code} {name}】股利與殖利率資訊\n\n"
        if pe_info:
            msg += pe_info
            
        yield_elem = soup.find(string=re.compile('殖利率'))
        if yield_elem:
            msg += f"📊 {yield_elem.parent.parent.text}\n"
        
        rows = soup.find_all('li', class_=re.compile(r'List\(n\)'))
        count = 0
        detail_msg = ""
        for row in rows:
            texts = list(row.stripped_strings)
            if len(texts) >= 5: 
                period = texts[0]
                cash = texts[1]
                yld = texts[3]
                ex_date = texts[5] if len(texts) > 5 else '-'
                pay_date = texts[7] if len(texts) > 7 else '-'
                detail_msg += f"\n🗓️ {period}\n現金股利: {cash} 元 | 殖利率: {yld}\n除息日: {ex_date} | 發放日: {pay_date}\n"
                count += 1
                if count >= 3:
                    break
                    
        if detail_msg:
            msg += "\n" + "-" * 15 + detail_msg
        else:
            if not yield_elem:
                msg += "⚠️ 查無近期配息資料。"
            
        return msg.strip()
    except Exception as e:
        print(f"Dividend Error: {e}")
        return "⚠️ 獲取資料失敗"

# ---- Flex Message Builder ----
def create_stock_bubble(data):
    if not data:
        return None
        
    price = data['price']
    change = data['change']
    change_pct = data['change_pct']
    
    if change > 0:
        color = "#ff3333" 
        sign = "+"
        trend = "▲"
    elif change < 0:
        color = "#00b300"
        sign = "-"
        trend = "▼"
    else:
        color = "#666666"
        sign = ""
        trend = "➖"
        
    symbol_str = data['symbol'].replace('.TW', '')
    
    if symbol_str == 't00':
        yahoo_symbol = '%5ETWII'
    elif symbol_str.startswith('otc'):
        yahoo_symbol = symbol_str.replace('otc', '')
    else:
        yahoo_symbol = symbol_str
        
    if data.get('is_crypto'):
        if symbol_str == 'USDT':
            chart_url = "https://tw.tradingview.com/chart/?symbol=BINANCE:USDTUSD"
        else:
            chart_url = f"https://tw.tradingview.com/chart/?symbol=BINANCE:{symbol_str}USDT"
    elif data.get('is_futures'):
        tv_map = {'TXF': 'TXF1!', 'MTX': 'MTX1!', 'TE': 'TE1!', 'TF': 'TF1!'}
        tv_sym = tv_map.get(symbol_str, f'{symbol_str}1!')
        chart_url = f"https://tw.tradingview.com/chart/?symbol=TAIFEX:{tv_sym}"
    elif data.get('is_us'):
        chart_url = f"https://tw.tradingview.com/chart/?symbol={symbol_str}"
        if symbol_str.startswith('^'):
            chart_url = f"https://tw.tradingview.com/chart/?symbol={symbol_str.replace('^', '')}"
    else:
        chart_url = f"https://tw.stock.yahoo.com/quote/{yahoo_symbol}/technical-analysis"
        
    return {
      "type": "bubble",
      "size": "micro",
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": data['name'],
            "weight": "bold",
            "size": "sm",
            "wrap": True
          },
          {
            "type": "text",
            "text": f"{price:,.2f}",
            "weight": "bold",
            "size": "xl",
            "color": color,
            "margin": "md"
          },
          {
            "type": "text",
            "text": f"{trend} {sign}{abs(change):,.2f} ({sign}{abs(change_pct):.2f}%)",
            "size": "xs",
            "color": color
          }
        ],
        "spacing": "sm",
        "paddingAll": "15px"
      },
      "footer": {
        "type": "box",
        "layout": "vertical",
        "spacing": "sm",
        "contents": [
          {
            "type": "button",
            "style": "link",
            "height": "sm",
            "action": {
              "type": "uri",
              "label": "走勢與K線",
              "uri": chart_url
            }
          }
        ],
        "flex": 0
      }
    }

# ---- Alerts Check ----
def check_price_alerts():
    alerts = get_alerts()
    if not alerts:
        return
        
    codes = list(set([a[2] for a in alerts]))
    
    current_prices = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_data, code): code for code in codes}
        for future in concurrent.futures.as_completed(futures):
            code = futures[future]
            try:
                data = future.result()
                if data:
                    current_prices[code] = data['price']
            except:
                pass
                
    for alert in alerts:
        a_id, user_id, code, condition, target_price = alert
        if code not in current_prices:
            continue
            
        curr_price = current_prices[code]
        triggered = False
        
        if condition == '>' and curr_price > target_price:
            triggered = True
        elif condition == '<' and curr_price < target_price:
            triggered = True
        elif condition == '>=' and curr_price >= target_price:
            triggered = True
        elif condition == '<=' and curr_price <= target_price:
            triggered = True
            
        if triggered:
            msg = f"🔔 【到價提醒】\n{code} 目前價格為 {curr_price:,.2f}，已達標 ({condition} {target_price})！"
            try:
                line_bot_api.push_message(user_id, TextSendMessage(text=msg))
                del_alert(a_id)
            except Exception as e:
                print(f"Failed to push message: {e}")

def daily_summary_push():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, favorites FROM users WHERE push_enabled=1 AND favorites IS NOT NULL AND favorites != ""')
    rows = c.fetchall()
    conn.close()
    
    for row in rows:
        uid, favs = row
        codes = [cd.strip() for cd in favs.split(',') if cd.strip()]
        if not codes:
            continue
            
        bubbles = []
        for code in codes[:12]:
            data = fetch_data(code)
            bubble = create_stock_bubble(data)
            if bubble:
                bubbles.append(bubble)
                
        if bubbles:
            carousel = {"type": "carousel", "contents": bubbles}
            alt_text = "📊 盤後總結"
            msg = FlexSendMessage(alt_text=alt_text, contents=carousel)
            try:
                line_bot_api.push_message(uid, msg)
            except Exception as e:
                print(f"Failed to push daily summary: {e}")

scheduler = BackgroundScheduler()
scheduler.add_job(func=check_price_alerts, trigger="interval", minutes=1)
scheduler.add_job(func=daily_summary_push, trigger="cron", hour=13, minute=35, timezone="Asia/Taipei")
scheduler.add_job(func=update_stock_names_db, trigger="cron", hour=4, minute=0, timezone="Asia/Taipei")
scheduler.start()


@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    handler.handle(body, signature)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id
    
    def parse_cmd(cmd_names, txt):
        if isinstance(cmd_names, str):
            cmd_names = [cmd_names]
        for cmd in cmd_names:
            if txt == cmd:
                return True, ""
            if txt.startswith(cmd + " "):
                return True, txt[len(cmd)+1:].strip()
            if txt.startswith(cmd):
                rest = txt[len(cmd):]
                if rest and ord(rest[0]) < 128 and rest[0].isalnum():
                    return True, rest
        return False, None

    # 1. News Command
    is_match, arg = parse_cmd('新聞', text)
    if is_match:
        if not arg:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入股票代碼，例如：新聞 2330"))
            return
        code = resolve_stock_code(arg)
        news_text = fetch_news(code)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"📰 【{code} 最新新聞】\n\n{news_text}"))
        return
        
    # Advanced Data Commands
    if text == '熱門股':
        msg = get_hot_stocks_msg()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return
        
    is_match, arg = parse_cmd('籌碼', text)
    if is_match:
        if not arg:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入股票代碼，例如：籌碼 2330"))
            return
        code = resolve_stock_code(arg)
        msg = get_institutional(code)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return
        
    is_match, arg = parse_cmd(['股利', '基本面'], text)
    if is_match:
        if not arg:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入股票代碼，例如：股利 2330"))
            return
        code = resolve_stock_code(arg)
        msg = get_dividend_info(code)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return
        
    # 2. Alerts Commands
    is_match, arg = parse_cmd('提醒', text)
    if is_match:
        if not arg:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="設定格式錯誤！請輸入例如：提醒 2330 > 1000"))
            return
        
        import re
        arg_norm = re.sub(r'([><=]+)', r' \1 ', arg)
        parts = arg_norm.split()
        
        if len(parts) >= 3:
            code = resolve_stock_code(parts[0].strip())
            if not fetch_data(code):
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ 查無標的「{parts[0].strip()}」，設定提醒失敗。"))
                return
            cond = parts[1]
            try:
                price = float(parts[2])
                if cond in ['>', '<', '>=', '<=']:
                    add_alert(user_id, code, cond, price)
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ 已設定提醒：當 {code} {cond} {price} 時通知您！"))
                    return
            except:
                pass
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="設定格式錯誤！請輸入例如：提醒 2330 > 1000"))
        return
        
    if text == '我的提醒':
        alerts = get_alerts(user_id)
        if not alerts:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="您目前沒有設定任何提醒。"))
            return
            
        reply = "🔔 【您的到價提醒】\n"
        for a in alerts:
            reply += f"[{a[0]}] {a[1]} {a[2]} {a[3]}\n"
        reply += "\n若要刪除提醒，請輸入「刪除提醒 數字代號」\n若要更改排序，請輸入「排序提醒 代號1,代號2...」"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return
        
    is_match, arg = parse_cmd('刪除提醒', text)
    if is_match:
        if not arg:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="刪除格式錯誤！請輸入例如：刪除提醒 1"))
            return
        try:
            a_id = int(arg)
            del_alert(a_id, user_id)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ 已刪除提醒代號 {a_id}"))
        except:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="刪除格式錯誤！請輸入例如：刪除提醒 1"))
        return
        
    is_match, arg = parse_cmd('排序提醒', text)
    if is_match:
        if not arg:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入提醒代號，例如：排序提醒 3,1,2"))
            return
            
        order_ids = [int(x.strip()) for x in arg.replace('，', ',').split(',') if x.strip().isdigit()]
        if not order_ids:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入提醒代號，例如：排序提醒 3,1,2"))
            return
            
        alerts = get_alerts(user_id)
        alerts_dict = {a[0]: a for a in alerts}
        
        valid = True
        for oid in order_ids:
            if oid not in alerts_dict:
                valid = False
        if not valid or len(order_ids) != len(alerts):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 輸入的代號數量不符或包含錯誤代號，請確認「我的提醒」清單。"))
            return
            
        for oid in order_ids:
            a = alerts_dict[oid]
            del_alert(oid, user_id)
            add_alert(user_id, a[1], a[2], a[3])
            
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="✅ 提醒排序已更新！輸入「我的提醒」查看最新排序。"))
        return


    # 4. Portfolio Commands
    is_match, arg = parse_cmd('買入', text)
    if is_match:
        parts = arg.split()
        if len(parts) >= 3:
            code = resolve_stock_code(parts[0])
            if not fetch_data(code):
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ 查無標的「{parts[0]}」，買入失敗。"))
                return
            try:
                cost = float(parts[1])
                shares = int(parts[2])
                update_portfolio(user_id, code, cost, shares)
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ 已紀錄買入 {code}！\n成本：{cost} 元，數量：{shares} 股"))
                return
            except:
                pass
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="格式錯誤！請輸入例如：買入 2330 900 1000"))
        return

    is_match, arg = parse_cmd('賣出', text)
    if is_match:
        parts = arg.split()
        if len(parts) >= 2:
            code = resolve_stock_code(parts[0])
            try:
                shares = int(parts[1])
                update_portfolio(user_id, code, 0, -shares) # Negative shares to sell, cost parameter is ignored for selling in our math since we just reduce shares
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ 已紀錄賣出 {code} 共 {shares} 股！"))
                return
            except:
                pass
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="格式錯誤！請輸入例如：賣出 2330 1000"))
        return
        
    is_match, arg = parse_cmd('清空庫存', text)
    if is_match:
        if arg:
            code = resolve_stock_code(arg.strip())
            clear_portfolio(user_id, code)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ 已清空 {code} 的庫存紀錄！"))
        else:
            clear_portfolio(user_id)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="✅ 已清空所有庫存紀錄！"))
        return

    if text == '我的庫存':
        holdings = get_portfolio(user_id)
        if not holdings:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="您目前沒有任何庫存紀錄。\n請輸入「買入 2330 900 1000」來新增！"))
            return
            
        reply = "💼 【我的庫存損益】\n"
        total_cost = 0
        total_value = 0
        
        for h in holdings:
            code, cost, shares = h
            data = fetch_data(code)
            if not data: continue
            
            current_price = data['price']
            h_cost = cost * shares
            h_value = current_price * shares
            h_pnl = h_value - h_cost
            h_pct = (h_pnl / h_cost * 100) if h_cost > 0 else 0
            
            total_cost += h_cost
            total_value += h_value
            
            sign = "+" if h_pnl > 0 else ""
            reply += f"--------------------\n[{data['name']} ({data['symbol']})]\n"
            reply += f"庫存：{shares:,} 股 | 均價：{cost:,.2f}\n"
            reply += f"現價：{current_price:,.2f} | 帳面：{sign}{h_pnl:,.0f} ({sign}{h_pct:.2f}%)\n"
            
        total_pnl = total_value - total_cost
        t_sign = "+" if total_pnl > 0 else ""
        t_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
        
        reply += f"--------------------\n"
        reply += f"總成本：{total_cost:,.0f}\n"
        reply += f"總現值：{total_value:,.0f}\n"
        reply += f"總損益：{t_sign}{total_pnl:,.0f} ({t_sign}{t_pct:.2f}%)"
        
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return


    # 3. Favorites & Push Commands
    if text == '開啟推播':
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('INSERT INTO users (id, push_enabled) VALUES (?, 1) ON CONFLICT(id) DO UPDATE SET push_enabled=1', (user_id,))
        conn.commit()
        conn.close()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="✅ 已開啟盤後總結自動推播！每日下午 1:35 將為您整理持股狀況。"))
        return
        
    if text == '關閉推播':
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('UPDATE users SET push_enabled=0 WHERE id=?', (user_id,))
        conn.commit()
        conn.close()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="✅ 已關閉自動推播。"))
        return

    is_match, arg = parse_cmd('設定最愛', text)
    if is_match:
        if not arg:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入要設定的代碼，例如：設定最愛 AAPL,2330,2317"))
            return
        raw_codes = [c.strip() for c in arg.replace('，', ',').split(',') if c.strip()]
        new_codes = []
        invalid_codes = []
        for c in raw_codes:
            code = resolve_stock_code(c)
            if fetch_data(code):
                new_codes.append(code)
            else:
                invalid_codes.append(c)
                
        if invalid_codes:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ 以下標的查無資料，請確認後再試：\n{', '.join(invalid_codes)}"))
            return
            
        set_favorites(user_id, new_codes)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ 已重新設定！目前的我的最愛排序為：\n{', '.join(new_codes)}"))
        return
        
    is_match, arg = parse_cmd(['加入最愛', '新增最愛'], text)
    if is_match:
        if not arg:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入要加入的代碼，例如：加入最愛 2330,2317"))
            return
        raw_codes = [c.strip() for c in arg.replace('，', ',').split(',') if c.strip()]
        new_codes = []
        invalid_codes = []
        for c in raw_codes:
            code = resolve_stock_code(c)
            if fetch_data(code):
                new_codes.append(code)
            else:
                invalid_codes.append(c)
                
        if invalid_codes:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ 以下標的查無資料，加入失敗：\n{', '.join(invalid_codes)}"))
            return
            
        favs = get_favorites(user_id)
        for code in new_codes:
            if code not in favs:
                favs.append(code)
        set_favorites(user_id, favs)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ 已加入！目前的我的最愛：\n{', '.join(favs)}"))
        return
        
    is_match, arg = parse_cmd('刪除最愛', text)
    if is_match:
        if not arg:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入要刪除的代碼，例如：刪除最愛 2330"))
            return
        del_codes = [resolve_stock_code(c.strip()) for c in arg.replace('，', ',').split(',') if c.strip()]
        favs = get_favorites(user_id)
        favs = [f for f in favs if f not in del_codes]
        set_favorites(user_id, favs)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🗑️ 已刪除！目前的我的最愛：\n{', '.join(favs) if favs else '空'}"))
        return
        
    if text == '清空最愛':
        set_favorites(user_id, [])
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🗑️ 我的最愛已全部清空！"))
        return
        
    # 4. Query Commands
    codes = []
    if text == '我的最愛':
        codes = get_favorites(user_id)
        if not codes:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="您目前沒有設定我的最愛喔！\n請輸入「加入最愛 2330,2317」來新增。"))
            return
            
    elif text in ['大盤', '台指', '加權指數']:
        codes = ['大盤']
    elif text in ['美股', '指數', '國際指數']:
        codes = ['道瓊', '那斯達克', '標普', '費半']
    elif text in ['加密貨幣', 'crypto']:
        codes = ['BTC', 'ETH']
    elif text == '夜盤':
        codes = ['TXF', 'MTX']

    elif text == '指令表':
        quick_reply = QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="🌟 我的最愛", text="我的最愛")),
            QuickReplyButton(action=MessageAction(label="📊 查大盤", text="大盤")),
            QuickReplyButton(action=MessageAction(label="🌎 查國際指數", text="國際指數")),
            QuickReplyButton(action=MessageAction(label="🪙 查加密貨幣", text="加密貨幣"))
        ])
        reply = (
            "📖 【StockBot 完整使用教學】\n"
            "💡 提示：所有指令皆支援「股票代碼」或「中文名稱」 (例: 2330 或 台積電)\n\n"
            "🔍 1. 查詢功能\n"
            "• 查報價：直接輸入代碼/名稱 (例: 台積電, AAPL, BTC)\n"
            "• 熱門排行：輸入「熱門股」\n"
            "• 籌碼追蹤：輸入「籌碼 台積電」(支援上市櫃/ETF)\n"
            "• 基本面與配息：輸入「股利 00878」(支援上市櫃/ETF)\n"
            "• 最新新聞：輸入「新聞 台積電」\n"
            "• 夜盤查詢：輸入「夜盤」(台指期+小台)\n"
            "• 指定期貨：輸入「期TXF」「期MTX」\n"
            "• 美股盤後：輸入「美股盤後」(AAPL, NVDA, TSLA...)\n\n"
            "🌟 2. 我的最愛 (自訂輪播卡片)\n"
            "• 查詢最愛：輸入「我的最愛」\n"
            "• 新增最愛：加入最愛 鴻海, AAPL\n"
            "• 自訂排序：設定最愛 AAPL, 台積電\n"
            "• 刪除最愛：刪除最愛 台積電\n\n"
            "🔔 3. 到價提醒 & 盤後推播\n"
            "• 盤後推播：輸入「開啟推播」或「關閉推播」(每日13:35自動私訊最愛總結)\n"
            "• 新增提醒：提醒 聯發科 > 1050\n"
            "• 查詢/刪除提醒：輸入「我的提醒」\n"
"• 新增提醒：提醒 聯發科 > 1050\n"
            "• 查詢/刪除提醒：輸入「我的提醒」\n"
            "• 更改提醒排序：排序提醒 3,1,2\n\n"
            "💼 4. 庫存損益追蹤\n"
            "• 買入紀錄：買入 2330 900 1000 (代碼 成本 股數)\n"
            "• 賣出紀錄：賣出 2330 500 (代碼 股數)\n"
            "• 查詢庫存：輸入「我的庫存」\n"
            "• 清空庫存：清空庫存 2330 (可選)"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply, quick_reply=quick_reply))
        return
        
    else:
        if text.upper().startswith('K') and len(text) > 1 and text[1].isdigit():
            text = text[1:]
        if text.startswith('期') and len(text) > 1:
            clean_text = text[1:]
        elif text.startswith('股'):
            clean_text = text[1:]
        else:
            clean_text = text
        raw_codes = [c.strip() for c in clean_text.replace('，', ',').split(',') if c.strip()]
        codes = [resolve_stock_code(c) for c in raw_codes]
        
    if not codes:
        return 

    # Fetch concurrently
    bubbles = []
    
    results_map = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_data, code): code for code in codes}
        for future in concurrent.futures.as_completed(futures):
            code = futures[future]
            try:
                data = future.result()
                if data:
                    results_map[code] = data
            except:
                pass
                
    for code in codes:
        if code in results_map:
            bubble = create_stock_bubble(results_map[code])
            if bubble:
                bubbles.append(bubble)
                
    if not bubbles:
        if text == '我的最愛':
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 您的最愛清單中的股票目前查詢失敗。"))
        elif text in ('夜盤', '美股盤後'):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ 【{text}】資料暫無，請稍後再試"))
        return

    if len(bubbles) > 12:
        bubbles = bubbles[:12]
        
    flex_message = FlexSendMessage(
        alt_text="股票報價",
        contents={
            "type": "carousel",
            "contents": bubbles
        }
    )
    
    line_bot_api.reply_message(event.reply_token, flex_message)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
