from flask import Flask, request
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import yfinance as yf
import os

app = Flask(__name__)

line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))

def get_stock_info(symbol):
    try:
        stock = yf.Ticker(symbol)
        data = stock.info
        current = data.get('currentPrice')
        previous_close = data.get('previousClose')
        if not current or not previous_close:
            return None
        change = current - previous_close
        change_pct = (change / previous_close) * 100
        arrow = "📈" if change >= 0 else "📉"
        sign = "+" if change >= 0 else ""
        return f"{symbol}: NT${current:.2f} {arrow} {sign}{change_pct:.2f}%"
    except:
        return None

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    handler.handle(body, signature)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    if text.startswith('股'):
        codes = text[1:].split(',')
        results = []
        for code in codes:
            code = code.strip()
            if not code:
                continue
            if '.' not in code:
                code = code + '.TW'
            info = get_stock_info(code)
            results.append(info if info else f"❌ {code} 找不到")
        reply = '\n'.join(results) if results else "請輸入正確的股票代碼"
    else:
        reply = "📈 股價查詢\n輸入『股代碼』查詢\n\n例子：\n股2330\n股2330,2454,5480"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
