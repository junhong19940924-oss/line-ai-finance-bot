import os
import re

from flask import Flask, abort, request
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from supabase import Client, create_client


app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

handler = WebhookHandler(LINE_CHANNEL_SECRET)
configuration = Configuration(
    access_token=LINE_CHANNEL_ACCESS_TOKEN
)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def classify_expense(description: str) -> str:
    text = description.lower()

    categories = {
        "餐飲": ["早餐", "午餐", "晚餐", "飲料", "咖啡", "便當", "麥當勞"],
        "交通": ["加油", "停車", "計程車", "捷運", "火車", "高鐵"],
        "購物": ["衣服", "鞋子", "蝦皮", "購物"],
        "娛樂": ["電影", "遊戲", "唱歌"],
        "生活": ["水費", "電費", "瓦斯", "房租", "電話費"],
    }

    for category, keywords in categories.items():
        if any(keyword in text for keyword in keywords):
            return category

    return "其他"


def parse_transaction(text: str):
    match = re.search(r"(.+?)\s*([0-9,]+)\s*元?$", text.strip())

    if not match:
        return None

    description = match.group(1).strip()
    amount = int(match.group(2).replace(",", ""))

    income_keywords = ["薪水", "獎金", "收入", "退款", "賺"]
    transaction_type = (
        "收入"
        if any(keyword in description for keyword in income_keywords)
        else "支出"
    )

    category = (
        "收入"
        if transaction_type == "收入"
        else classify_expense(description)
    )

    return {
        "type": transaction_type,
        "category": category,
        "amount": amount,
        "description": description,
    }


@app.route("/", methods=["GET"])
def home():
    return "LINE AI Finance Bot is running."


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    if not signature:
        abort(400)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text.strip()
    transaction = parse_transaction(user_text)

    if transaction is None:
        reply_text = (
            "我看不懂這筆記帳。\n\n"
            "請輸入像這樣：\n"
            "早餐 85\n"
            "加油 500\n"
            "薪水 70000"
        )
    else:
        user_id = event.source.user_id

        supabase.table("transactions").insert(
            {
                "line_user_id": user_id,
                "type": transaction["type"],
                "category": transaction["category"],
                "amount": transaction["amount"],
                "description": transaction["description"],
            }
        ).execute()

        sign = "+" if transaction["type"] == "收入" else "-"

        reply_text = (
            f"✅ 已記錄{transaction['type']}\n"
            f"分類：{transaction['category']}\n"
            f"項目：{transaction['description']}\n"
            f"金額：{sign} NT${transaction['amount']:,}"
        )

    with ApiClient(configuration) as api_client:
        messaging_api = MessagingApi(api_client)
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)],
            )
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
