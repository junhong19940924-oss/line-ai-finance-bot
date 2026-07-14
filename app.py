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
        return """
    <!DOCTYPE html>
    <html lang="zh-Hant">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>AI 財務管家</title>

        <style>
            * {
                box-sizing: border-box;
                margin: 0;
                padding: 0;
            }

            body {
                font-family: Arial, "Microsoft JhengHei", sans-serif;
                background: #f4f6f9;
                color: #1f2937;
            }

            .header {
                background: linear-gradient(135deg, #0f766e, #14b8a6);
                color: white;
                padding: 28px 20px;
            }

            .header-content {
                max-width: 1100px;
                margin: auto;
            }

            .header h1 {
                font-size: 28px;
                margin-bottom: 8px;
            }

            .header p {
                opacity: 0.9;
            }

            .container {
                max-width: 1100px;
                margin: 25px auto;
                padding: 0 16px;
            }

            .cards {
                display: grid;
                grid-template-columns: repeat(4, 1fr);
                gap: 16px;
                margin-bottom: 22px;
            }

            .card {
                background: white;
                border-radius: 14px;
                padding: 20px;
                box-shadow: 0 4px 14px rgba(0, 0, 0, 0.06);
            }

            .card-title {
                color: #6b7280;
                font-size: 14px;
                margin-bottom: 12px;
            }

            .card-value {
                font-size: 27px;
                font-weight: bold;
            }

            .income {
                color: #059669;
            }

            .expense {
                color: #dc2626;
            }

            .balance {
                color: #2563eb;
            }

            .debt {
                color: #d97706;
            }

            .section {
                background: white;
                border-radius: 14px;
                padding: 22px;
                margin-bottom: 20px;
                box-shadow: 0 4px 14px rgba(0, 0, 0, 0.06);
            }

            .section h2 {
                font-size: 19px;
                margin-bottom: 18px;
            }

            table {
                width: 100%;
                border-collapse: collapse;
            }

            th,
            td {
                padding: 13px 10px;
                border-bottom: 1px solid #e5e7eb;
                text-align: left;
            }

            th {
                color: #6b7280;
                font-size: 14px;
            }

            .status {
                display: inline-block;
                background: #d1fae5;
                color: #047857;
                padding: 5px 10px;
                border-radius: 20px;
                font-size: 13px;
            }

            .ai-box {
                background: #ecfeff;
                border-left: 5px solid #14b8a6;
                border-radius: 10px;
                padding: 18px;
                line-height: 1.8;
            }

            .footer {
                text-align: center;
                color: #9ca3af;
                padding: 10px 20px 30px;
                font-size: 13px;
            }

            @media (max-width: 850px) {
                .cards {
                    grid-template-columns: repeat(2, 1fr);
                }
            }

            @media (max-width: 520px) {
                .cards {
                    grid-template-columns: 1fr;
                }

                .header h1 {
                    font-size: 23px;
                }

                .card-value {
                    font-size: 24px;
                }

                table {
                    font-size: 13px;
                }
            }
        </style>
    </head>

    <body>
        <div class="header">
            <div class="header-content">
                <h1>AI 財務管家</h1>
                <p>記帳、收支分析與負債管理</p>
            </div>
        </div>

        <div class="container">
            <div class="cards">
                <div class="card">
                    <div class="card-title">本月收入</div>
                    <div class="card-value income">NT$ 0</div>
                </div>

                <div class="card">
                    <div class="card-title">本月支出</div>
                    <div class="card-value expense">NT$ 0</div>
                </div>

                <div class="card">
                    <div class="card-title">本月結餘</div>
                    <div class="card-value balance">NT$ 0</div>
                </div>

                <div class="card">
                    <div class="card-title">總負債</div>
                    <div class="card-value debt">NT$ 0</div>
                </div>
            </div>

            <div class="section">
                <h2>最近記帳紀錄</h2>

                <table>
                    <thead>
                        <tr>
                            <th>日期</th>
                            <th>項目</th>
                            <th>分類</th>
                            <th>金額</th>
                        </tr>
                    </thead>

                    <tbody>
                        <tr>
                            <td>尚無資料</td>
                            <td>請從 LINE 輸入記帳內容</td>
                            <td>—</td>
                            <td>NT$ 0</td>
                        </tr>
                    </tbody>
                </table>
            </div>

            <div class="section">
                <h2>LINE Bot 狀態</h2>
                <span class="status">系統運作中</span>
                <p style="margin-top: 12px; color: #6b7280;">
                    可在 LINE 輸入：「早餐 85」、「加油 500」或「薪水 70000」。
                </p>
            </div>

            <div class="section">
                <h2>AI 財務建議</h2>

                <div class="ai-box">
                    目前尚未取得足夠的收支資料。開始使用 LINE 記帳後，
                    系統將根據你的收入、支出及負債狀況提供分析。
                </div>
            </div>
        </div>

        <div class="footer">
            AI Finance Manager · Powered by LINE Bot
        </div>
    </body>
    </html>
    """


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
