import os
import re
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

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

supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_KEY,
)

def reply_line(event, text: str) -> None:
    """Send exactly one LINE reply for the current event."""
    with ApiClient(configuration) as api_client:
        messaging_api = MessagingApi(api_client)
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=text)],
            )
        )


def classify_expense(description: str) -> str:
    text = description.lower()

    category_keywords = {
        "餐飲": [
            "早餐", "午餐", "晚餐", "宵夜", "飲料", "咖啡",
            "便當", "餐廳", "吃飯", "食物", "麥當勞",
        ],
        "交通": [
            "加油", "停車", "計程車", "uber", "高鐵",
            "火車", "捷運", "公車", "車票", "過路費",
        ],
        "購物": [
            "衣服", "鞋子", "購物", "網購", "蝦皮",
            "momo", "生活用品", "日用品",
        ],
        "娛樂": [
            "電影", "遊戲", "唱歌", "ktv", "旅遊",
            "住宿", "門票",
        ],
        "醫療": [
            "看醫生", "掛號", "藥", "診所", "醫院",
            "牙醫", "保健",
        ],
        "居家": [
            "房租", "水費", "電費", "瓦斯", "網路",
            "電話費", "管理費",
        ],
        "保險": ["保險", "保費"],
        "貸款": ["車貸", "信貸", "房貸", "貸款", "還款"],
    }

    for category, keywords in category_keywords.items():
        if any(keyword in text for keyword in keywords):
            return category

    return "其他"


def parse_transaction(user_text: str):
    text = user_text.strip()

    amount_match = re.search(
        r"(-?\d[\d,]*(?:\.\d+)?)",
        text
    )

    if not amount_match:
        return None

    try:
        amount = float(
            amount_match.group(1).replace(",", "")
        )
    except ValueError:
        return None

    if amount <= 0:
        return None

    description = (
        text[:amount_match.start()]
        + text[amount_match.end():]
    ).strip()

    description = re.sub(
        r"[元塊\$NTnt：:，,\s]+$",
        "",
        description,
    ).strip()

    if not description:
        description = "未填寫項目"

    income_keywords = [
        "薪水",
        "薪資",
        "收入",
        "獎金",
        "年終",
        "兼職",
        "利息",
        "股息",
        "退款",
        "入帳",
        "收款",
    ]

    transaction_type = (
        "收入"
        if any(keyword in text for keyword in income_keywords)
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
    try:
        response = (
            supabase
            .table("transactions")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        )

        transactions = response.data or []

    except Exception as error:
        print("Dashboard 讀取 Supabase 失敗：", error)
        transactions = []

    try:
        debt_response = (
            supabase
            .table("debts")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        )

        debts = debt_response.data or []

    except Exception as error:
        print("Dashboard 讀取負債失敗：", error)
        debts = []

    total_debt = sum(
        float(item.get("remaining_amount") or 0)
        for item in debts
    )

    debt_records = []

    for debt in debts:
        debt_records.append(
            {
                "created_at": debt.get("created_at", ""),
                "type": "負債",
                "category": debt.get("debt_type") or "其他",
                "description": debt.get("debt_name") or "未填寫",
                "amount": debt.get("remaining_amount") or 0,
            }
        )

    recent_items = transactions + debt_records

    recent_items.sort(
        key=lambda item: str(item.get("created_at", "")),
        reverse=True,
    )

    taiwan_now = datetime.now(
        ZoneInfo("Asia/Taipei")
    )

    current_month = taiwan_now.strftime("%Y-%m")

    monthly_income = 0
    monthly_expense = 0

    for item in transactions:
        created_at = str(
            item.get("created_at", "")
        )

        if not created_at.startswith(current_month):
            continue

        amount = float(
            item.get("amount") or 0
        )

        if item.get("type") == "收入":
            monthly_income += amount

        elif item.get("type") == "支出":
            monthly_expense += amount

    monthly_balance = monthly_income - monthly_expense

    recent_rows = ""

    for item in recent_items[:10]:
        created_at = str(
            item.get("created_at", "")
        )[:10]

        description = escape(
            str(item.get("description") or "未填寫")
        )

        category = escape(
            str(item.get("category") or "未分類")
        )

        transaction_type = str(
            item.get("type") or ""
        )

        amount = float(
            item.get("amount") or 0
        )

        if transaction_type == "收入":
            amount_sign = "+"
            amount_class = "income"
        elif transaction_type == "負債":
            amount_sign = ""
            amount_class = "debt"
        else:
            amount_sign = "-"
            amount_class = "expense"

        recent_rows += f"""
        <tr>
            <td>{created_at}</td>
            <td>{description}</td>
            <td>{category}</td>
            <td class="{amount_class}">
                {amount_sign} NT$ {amount:,.0f}
            </td>
        </tr>
        """

    if not recent_rows:
        recent_rows = """
        <tr>
            <td>尚無資料</td>
            <td>請從 LINE 輸入記帳內容</td>
            <td>—</td>
            <td>NT$ 0</td>
        </tr>
        """

    if monthly_income == 0 and monthly_expense == 0:
        ai_advice = (
            "目前尚未取得本月收支資料。"
            "請先從 LINE 輸入記帳內容。"
        )

    elif monthly_expense > monthly_income:
        ai_advice = (
            "本月支出目前高於收入，"
            "建議先檢查非必要支出，"
            "並設定每週可使用的預算。"
        )

    elif monthly_expense >= monthly_income * 0.8:
        ai_advice = (
            "本月支出已接近收入的 80%，"
            "建議控制接下來的娛樂及購物支出。"
        )

    else:
        savings_rate = (
            monthly_balance / monthly_income * 100
            if monthly_income > 0
            else 0
        )

        ai_advice = (
            f"本月目前結餘 NT$ {monthly_balance:,.0f}，"
            f"結餘率約 {savings_rate:.0f}%。"
            "建議保留一部分作為緊急預備金。"
        )

    html = f"""
    <!DOCTYPE html>
    <html lang="zh-Hant">
    <head>
        <meta charset="UTF-8">
        <meta
            name="viewport"
            content="width=device-width, initial-scale=1.0"
        >

        <title>AI 財務管家</title>

        <style>
            * {{
                box-sizing: border-box;
                margin: 0;
                padding: 0;
            }}

            body {{
                font-family: Arial, "Microsoft JhengHei", sans-serif;
                background: #f4f6f9;
                color: #1f2937;
            }}

            .header {{
                background: linear-gradient(
                    135deg,
                    #0f766e,
                    #14532d
                );
                color: white;
                padding: 32px 20px;
            }}

            .container {{
                width: 92%;
                max-width: 1100px;
                margin: auto;
            }}

            .header h1 {{
                font-size: 30px;
                margin-bottom: 8px;
            }}

            .header p {{
                opacity: 0.9;
            }}

            .summary {{
                display: grid;
                grid-template-columns:
                    repeat(4, 1fr);
                gap: 16px;
                margin-top: 25px;
            }}

            .card {{
                background: white;
                border-radius: 14px;
                padding: 22px;
                box-shadow:
                    0 4px 15px
                    rgba(0, 0, 0, 0.06);
            }}

            .card-title {{
                color: #6b7280;
                font-size: 14px;
                margin-bottom: 10px;
            }}

            .amount {{
                font-size: 26px;
                font-weight: bold;
            }}

            .income {{
                color: #15803d;
            }}

            .expense {{
                color: #dc2626;
            }}

            .balance {{
                color: #2563eb;
            }}

            .debt {{
                color: #b45309;
            }}

            .section {{
                background: white;
                margin-top: 22px;
                padding: 24px;
                border-radius: 14px;
                box-shadow:
                    0 4px 15px
                    rgba(0, 0, 0, 0.05);
            }}

            .section h2 {{
                margin-bottom: 18px;
            }}

            table {{
                width: 100%;
                border-collapse: collapse;
            }}

            th,
            td {{
                padding: 13px;
                text-align: left;
                border-bottom:
                    1px solid #e5e7eb;
            }}

            th {{
                color: #6b7280;
                font-size: 14px;
            }}

            .status {{
                display: inline-block;
                background: #dcfce7;
                color: #166534;
                padding: 7px 12px;
                border-radius: 20px;
                font-size: 14px;
            }}

            .ai-box {{
                background: #f0fdfa;
                border-left:
                    5px solid #0f766e;
                padding: 18px;
                border-radius: 8px;
                line-height: 1.8;
            }}

            .footer {{
                text-align: center;
                color: #9ca3af;
                padding: 30px;
                font-size: 13px;
            }}

            @media (
                max-width: 800px
            ) {{
                .summary {{
                    grid-template-columns:
                        repeat(2, 1fr);
                }}
            }}

            @media (
                max-width: 500px
            ) {{
                .summary {{
                    grid-template-columns: 1fr;
                }}

                .amount {{
                    font-size: 22px;
                }}

                table {{
                    font-size: 13px;
                }}

                th,
                td {{
                    padding: 8px;
                }}
            }}
        </style>
    </head>

    <body>
        <div class="header">
            <div class="container">
                <h1>AI 財務管家</h1>

                <p>
                    記帳、收支分析與負債管理
                </p>
            </div>
        </div>

        <div class="container">
            <div class="summary">
                <div class="card">
                    <div class="card-title">
                        本月收入
                    </div>

                    <div class="amount income">
                        NT$ {monthly_income:,.0f}
                    </div>
                </div>

                <div class="card">
                    <div class="card-title">
                        本月支出
                    </div>

                    <div class="amount expense">
                        NT$ {monthly_expense:,.0f}
                    </div>
                </div>

                <div class="card">
                    <div class="card-title">
                        本月結餘
                    </div>

                    <div class="amount balance">
                        NT$ {monthly_balance:,.0f}
                    </div>
                </div>

                <div class="card">
                    <div class="card-title">
                        總負債
                    </div>

                    <div class="amount debt">
                        NT$ {total_debt:,.0f}
                    </div>
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
                        {recent_rows}
                    </tbody>
                </table>
            </div>

            <div class="section">
                <h2>LINE Bot 狀態</h2>

                <span class="status">
                    系統運作中
                </span>

                <p
                    style="
                        margin-top: 12px;
                        color: #6b7280;
                    "
                >
                    可在 LINE 輸入：
                    「早餐 85」、
                    「加油 500」或
                    「薪水 70000」。
                </p>
            </div>

            <div class="section">
                <h2>AI 財務建議</h2>

                <div class="ai-box">
                    {ai_advice}
                </div>
            </div>
        </div>

        <div class="footer">
            AI Finance Manager
            · Powered by LINE Bot
        </div>
    </body>
    </html>
    """

    return html


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get(
        "X-Line-Signature"
    )

    body = request.get_data(
        as_text=True
    )

    if not signature:
        abort(400)

    try:
        handler.handle(
            body,
            signature,
        )

    except InvalidSignatureError:
        abort(400)

    return "OK"


@handler.add(
    MessageEvent,
    message=TextMessageContent,
)
def handle_message(event):
    user_text = event.message.text.strip()
    user_id = event.source.user_id

    # 負債指令優先處理，避免被一般記帳解析器誤判成支出。
    # 可接受：
    # 新增負債 玉山信用卡 40000
    # 新增負債玉山信用卡40000
    # 負債 玉山信用卡 40,000
    if user_text.startswith(("新增負債", "負債")):
        debt_match = re.match(
            r"^(?:新增負債|負債)\\s*(.+?)\\s*(\\d[\\d,]*)\\s*$",
            user_text,
        )

        if not debt_match:
            reply_line(
                event,
                "格式錯誤\n請輸入：新增負債 名稱 金額\n"
                "例如：新增負債 玉山信用卡 40000",
            )
            return

        debt_name = debt_match.group(1).strip()

        try:
            amount = int(debt_match.group(2).replace(",", ""))
        except ValueError:
            reply_line(event, "金額格式錯誤\n例如：新增負債 玉山信用卡 40000")
            return

        if amount <= 0:
            reply_line(event, "負債金額必須大於 0")
            return

        debt_type = (
            "信用卡" if "卡" in debt_name
            else "車貸" if "車貸" in debt_name or "機車貸" in debt_name
            else "信貸" if "信貸" in debt_name or "信用貸款" in debt_name
            else "房貸" if "房貸" in debt_name
            else "其他"
        )

        try:
            supabase.table("debts").insert(
                {
                    "line_user_id": user_id,
                    "debt_name": debt_name,
                    "debt_type": debt_type,
                    "original_amount": amount,
                    "remaining_amount": amount,
                    "monthly_payment": 0,
                }
            ).execute()
        except Exception as error:
            print("新增負債失敗：", error)
            reply_line(event, "新增負債失敗，系統已記錄錯誤，請稍後再試。")
            return

        reply_line(
            event,
            f"💳 已新增負債\n"
            f"名稱：{debt_name}\n"
            f"類型：{debt_type}\n"
            f"剩餘金額：NT$ {amount:,}",
        )
        return

    transaction = parse_transaction(user_text)

    if transaction is None:
        reply_line(
            event,
            "我看不懂這筆記帳。\n\n"
            "請輸入像這樣：\n"
            "早餐 85\n"
            "加油 500\n"
            "薪水 70000\n"
            "新增負債 玉山信用卡 40000",
        )
        return

    try:
        supabase.table("transactions").insert(
            {
                "line_user_id": user_id,
                "type": transaction["type"],
                "category": transaction["category"],
                "amount": int(transaction["amount"]),
                "description": transaction["description"],
            }
        ).execute()
    except Exception as error:
        print("新增記帳失敗：", error)
        reply_line(event, "記帳失敗，系統已記錄錯誤，請稍後再試。")
        return

    sign = "+" if transaction["type"] == "收入" else "-"

    reply_line(
        event,
        f"✅ 已記錄{transaction['type']}\n"
        f"分類：{transaction['category']}\n"
        f"項目：{transaction['description']}\n"
        f"金額：{sign} NT$ {transaction['amount']:,.0f}",
    )


if __name__ == "__main__":
    port = int(
        os.environ.get(
            "PORT",
            5000,
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )
