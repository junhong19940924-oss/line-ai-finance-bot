import os
import re
from datetime import datetime
from html import escape
from typing import Any
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
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

TAIPEI = ZoneInfo("Asia/Taipei")


# ----------------------------
# 共用工具
# ----------------------------

def reply_line(event: MessageEvent, text: str) -> None:
    """Reply exactly once to the current LINE event."""
    with ApiClient(configuration) as api_client:
        messaging_api = MessagingApi(api_client)
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=text)],
            )
        )


def format_taipei_datetime(value: str | None) -> str:
    if not value:
        return "尚未更新"

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TAIPEI)
        return parsed.astimezone(TAIPEI).strftime("%Y/%m/%d %H:%M")
    except (ValueError, TypeError):
        return str(value)[:16].replace("T", " ")


def parse_positive_int(value: str) -> int | None:
    cleaned = value.replace(",", "").strip()
    if not cleaned.isdigit():
        return None

    amount = int(cleaned)
    return amount if amount > 0 else None


def classify_expense(description: str) -> str:
    text = description.lower()

    category_keywords = {
        "飲食": [
            "早餐", "午餐", "晚餐", "宵夜", "飲料", "咖啡",
            "便當", "餐廳", "吃飯", "食物", "麥當勞",
        ],
        "信用卡刷卡": [
            "刷卡", "信用卡", "卡費消費",
        ],
        "加油": [
            "加油", "汽油", "柴油",
        ],
        "交通": [
            "停車", "計程車", "uber", "高鐵",
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


def classify_debt(debt_name: str) -> str:
    if "卡" in debt_name:
        return "信用卡"
    if "車貸" in debt_name or "機車貸" in debt_name:
        return "車貸"
    if "信貸" in debt_name or "信用貸款" in debt_name:
        return "信貸"
    if "房貸" in debt_name:
        return "房貸"
    return "其他"


def classify_income(description: str) -> str:
    text = description.lower()

    if any(keyword in text for keyword in ["獎金", "年終", "績效", "紅利"]):
        return "獎金"
    if any(keyword in text for keyword in ["兼職", "外快", "接案"]):
        return "兼職"
    if any(keyword in text for keyword in ["股息", "利息", "配息"]):
        return "投資收入"
    if any(keyword in text for keyword in ["退款", "退費"]):
        return "退款"
    if any(keyword in text for keyword in ["薪水", "薪資", "月薪", "工資"]):
        return "薪水"

    return "其他收入"


def parse_transaction(user_text: str) -> dict[str, Any] | None:
    text = user_text.strip()

    amount_match = re.search(r"(-?\d[\d,]*(?:\.\d+)?)", text)
    if not amount_match:
        return None

    try:
        amount = float(amount_match.group(1).replace(",", ""))
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
        "薪水", "薪資", "收入", "獎金", "年終", "兼職",
        "利息", "股息", "退款", "入帳", "收款",
    ]

    transaction_type = (
        "收入"
        if any(keyword in text for keyword in income_keywords)
        else "支出"
    )

    category = (
        classify_income(description)
        if transaction_type == "收入"
        else classify_expense(description)
    )

    return {
        "type": transaction_type,
        "category": category,
        "amount": amount,
        "description": description,
    }


def normalize_account(value: str | None) -> str:
    account = (value or "").strip()
    if account in {"金家", "金家帳戶", "金家水電帳戶"}:
        return "金家水電"
    return account if account else "個人"


def get_account_transactions(
    account: str,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    query = (
        supabase
        .table("transactions")
        .select("*")
        .eq("account", account)
        .order("created_at", desc=True)
    )

    if user_id:
        query = query.eq("line_user_id", user_id)

    response = query.execute()
    return response.data or []


def get_account_opening_balance(account: str) -> int:
    response = (
        supabase
        .table("account_balances")
        .select("opening_balance")
        .eq("account", account)
        .limit(1)
        .execute()
    )

    rows = response.data or []
    return int(rows[0].get("opening_balance") or 0) if rows else 0


def calculate_account_balance(
    account: str,
    transactions: list[dict[str, Any]],
) -> int:
    opening_balance = get_account_opening_balance(account)
    net_change = 0

    for item in transactions:
        amount = int(item.get("amount") or 0)
        if item.get("type") == "收入":
            net_change += amount
        elif item.get("type") == "支出":
            net_change -= amount

    return opening_balance + net_change


def set_account_current_balance(
    account: str,
    current_balance: int,
    transactions: list[dict[str, Any]],
) -> None:
    net_change = 0

    for item in transactions:
        amount = int(item.get("amount") or 0)
        if item.get("type") == "收入":
            net_change += amount
        elif item.get("type") == "支出":
            net_change -= amount

    opening_balance = current_balance - net_change

    (
        supabase
        .table("account_balances")
        .upsert(
            {
                "account": account,
                "opening_balance": opening_balance,
                "updated_at": datetime.now(TAIPEI).isoformat(),
            },
            on_conflict="account",
        )
        .execute()
    )


PERSONAL_BANKS = (
    "玉山銀行",
    "中國信託",
    "渣打銀行",
    "華南銀行",
    "LINE Bank",
    "LINE Pay Money",
)
JINJIA_BANKS = ("王道銀行",)
ALL_BANKS = PERSONAL_BANKS + JINJIA_BANKS


BANK_ALIASES = {
    "玉山": "玉山銀行",
    "玉山銀行": "玉山銀行",
    "中信": "中國信託",
    "中信銀行": "中國信託",
    "中國信託": "中國信託",
    "中國信託銀行": "中國信託",
    "渣打": "渣打銀行",
    "渣打銀行": "渣打銀行",
    "華南": "華南銀行",
    "華南銀行": "華南銀行",
    "line bank": "LINE Bank",
    "linebank": "LINE Bank",
    "line pay money": "LINE Pay Money",
    "linepay money": "LINE Pay Money",
    "linepaymoney": "LINE Pay Money",
    "王道": "王道銀行",
    "王道銀行": "王道銀行",
}


def normalize_bank_name(value: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", value.strip())
    return BANK_ALIASES.get(cleaned.lower(), BANK_ALIASES.get(cleaned))



def get_bank_balances(owner: str) -> dict[str, dict[str, Any]]:
    expected = PERSONAL_BANKS if owner == "個人" else JINJIA_BANKS

    response = (
        supabase
        .table("bank_balances")
        .select("bank_name,balance,updated_at")
        .eq("owner", owner)
        .execute()
    )

    stored = {
        str(item.get("bank_name")): {
            "balance": int(item.get("balance") or 0),
            "updated_at": item.get("updated_at"),
        }
        for item in (response.data or [])
    }

    return {
        bank: stored.get(
            bank,
            {"balance": 0, "updated_at": None},
        )
        for bank in expected
    }


def set_bank_balance(owner: str, bank_name: str, balance: int) -> None:
    if balance < 0:
        raise ValueError("銀行餘額不可小於 0")

    now_text = datetime.now(TAIPEI).isoformat()

    current_response = (
        supabase
        .table("bank_balances")
        .select("balance")
        .eq("owner", owner)
        .eq("bank_name", bank_name)
        .limit(1)
        .execute()
    )
    current_rows = current_response.data or []
    old_balance = (
        int(current_rows[0].get("balance") or 0)
        if current_rows
        else 0
    )

    (
        supabase
        .table("bank_balances")
        .upsert(
            {
                "owner": owner,
                "bank_name": bank_name,
                "balance": balance,
                "updated_at": now_text,
            },
            on_conflict="owner,bank_name",
        )
        .execute()
    )

    if not current_rows or old_balance != balance:
        (
            supabase
            .table("bank_balance_history")
            .insert(
                {
                    "owner": owner,
                    "bank_name": bank_name,
                    "balance": balance,
                    "recorded_at": now_text,
                }
            )
            .execute()
        )


def get_bank_balance_history(
    bank_name: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    query = (
        supabase
        .table("bank_balance_history")
        .select("*")
        .order("recorded_at", desc=True)
        .limit(limit)
    )

    if bank_name:
        query = query.eq("bank_name", bank_name)

    response = query.execute()
    return response.data or []


def build_bank_balance_rows(
    balances: dict[str, dict[str, Any]],
    total: int,
    bar_class: str = "bank-bar",
) -> str:
    rows = ""

    for bank_name, values in balances.items():
        balance = int(values.get("balance") or 0)
        updated_at = format_taipei_datetime(values.get("updated_at"))
        percent = balance / total * 100 if total > 0 else 0

        rows += f"""
        <div class="bank-balance-item">
            <div class="summary-row">
                <span>{escape(bank_name)}</span>
                <strong>NT$ {balance:,.0f}</strong>
            </div>
            <div class="progress">
                <div class="progress-bar {bar_class}"
                     style="width:{percent:.1f}%"></div>
            </div>
            <div class="summary-row">
                <span class="muted">{percent:.1f}%</span>
                <span class="muted">更新：{escape(updated_at)}</span>
            </div>
        </div>
        """

    return rows



CREDIT_CARDS = (
    "玉山信用卡",
    "中信信用卡",
    "兆豐信用卡",
)


def get_credit_cards() -> dict[str, dict[str, Any]]:
    response = (
        supabase
        .table("credit_cards")
        .select(
            "card_name,total_limit,available_limit,"
            "statement_day,due_day,statement_amount,"
            "payment_status,payment_date,updated_at"
        )
        .execute()
    )

    stored = {
        str(item.get("card_name")): {
            "total_limit": int(item.get("total_limit") or 0),
            "available_limit": int(item.get("available_limit") or 0),
            "statement_day": int(item.get("statement_day") or 0),
            "due_day": int(item.get("due_day") or 0),
            "statement_amount": int(item.get("statement_amount") or 0),
            "payment_status": str(
                item.get("payment_status") or "未繳交"
            ),
            "payment_date": item.get("payment_date"),
            "updated_at": item.get("updated_at"),
        }
        for item in (response.data or [])
    }

    default = {
        "total_limit": 0,
        "available_limit": 0,
        "statement_day": 0,
        "due_day": 0,
        "statement_amount": 0,
        "payment_status": "未繳交",
        "payment_date": None,
        "updated_at": None,
    }

    return {
        card: stored.get(card, default.copy())
        for card in CREDIT_CARDS
    }


def set_credit_card_values(
    card_name: str,
    total_limit: int | None = None,
    available_limit: int | None = None,
    statement_day: int | None = None,
    due_day: int | None = None,
    statement_amount: int | None = None,
    payment_status: str | None = None,
) -> None:
    current = get_credit_cards().get(card_name, {})

    new_total = (
        int(current.get("total_limit") or 0)
        if total_limit is None
        else total_limit
    )
    new_available = (
        int(current.get("available_limit") or 0)
        if available_limit is None
        else available_limit
    )
    new_statement_day = (
        int(current.get("statement_day") or 0)
        if statement_day is None
        else statement_day
    )
    new_due_day = (
        int(current.get("due_day") or 0)
        if due_day is None
        else due_day
    )
    new_statement_amount = (
        int(current.get("statement_amount") or 0)
        if statement_amount is None
        else statement_amount
    )
    new_payment_status = (
        str(current.get("payment_status") or "未繳交")
        if payment_status is None
        else payment_status
    )

    if min(new_total, new_available, new_statement_amount) < 0:
        raise ValueError("金額不可小於 0")

    if new_total > 0 and new_available > new_total:
        raise ValueError("可用額度不可大於總額度")

    for day_value in (new_statement_day, new_due_day):
        if day_value and not 1 <= day_value <= 31:
            raise ValueError("日期必須介於 1～31 日")

    if new_payment_status not in {"已繳交", "未繳交"}:
        raise ValueError("繳款狀態只能是已繳交或未繳交")

    now_text = datetime.now(TAIPEI).isoformat()
    payment_date = (
        now_text
        if new_payment_status == "已繳交"
        else None
    )

    (
        supabase
        .table("credit_cards")
        .upsert(
            {
                "card_name": card_name,
                "total_limit": new_total,
                "available_limit": new_available,
                "statement_day": new_statement_day or None,
                "due_day": new_due_day or None,
                "statement_amount": new_statement_amount,
                "payment_status": new_payment_status,
                "payment_date": payment_date,
                "updated_at": now_text,
            },
            on_conflict="card_name",
        )
        .execute()
    )


def build_credit_card_rows(
    cards: dict[str, dict[str, Any]],
) -> tuple[str, int, int]:
    rows = ""
    total_limit_sum = 0
    available_limit_sum = 0

    for card_name, values in cards.items():
        total_limit = int(values.get("total_limit") or 0)
        available_limit = int(values.get("available_limit") or 0)
        statement_day = int(values.get("statement_day") or 0)
        due_day = int(values.get("due_day") or 0)
        statement_amount = int(values.get("statement_amount") or 0)
        payment_status = str(
            values.get("payment_status") or "未繳交"
        )
        updated_at = format_taipei_datetime(
            values.get("updated_at")
        )

        percent = (
            available_limit / total_limit * 100
            if total_limit > 0
            else 0
        )

        total_limit_sum += total_limit
        available_limit_sum += available_limit

        statement_text = (
            f"每月 {statement_day} 日"
            if statement_day
            else "未設定"
        )
        due_text = (
            f"每月 {due_day} 日"
            if due_day
            else "未設定"
        )
        status_class = (
            "paid" if payment_status == "已繳交" else "unpaid"
        )
        credit_state = (
            "credit-danger"
            if total_limit > 0 and percent < 20
            else "credit-warning"
            if total_limit > 0 and percent < 40
            else "credit-safe"
        )

        rows += f"""
        <div class="bank-balance-item credit-card-item {credit_state}">
            <div class="summary-row">
                <span>{escape(card_name)}</span>
                <strong>可用 NT$ {available_limit:,.0f}</strong>
            </div>
            <div class="muted" style="margin:6px 0;">
                總額度 NT$ {total_limit:,.0f}
                ｜本期應繳 NT$ {statement_amount:,.0f}
            </div>
            <div class="muted" style="margin-bottom:6px;">
                結帳日：{statement_text}｜繳款日：{due_text}
            </div>
            <div class="progress">
                <div class="progress-bar credit-card-bar"
                     style="width:{percent:.1f}%"></div>
            </div>
            <div class="summary-row">
                <span class="muted">可用比例 {percent:.1f}%</span>
                <span class="payment-status {status_class}">
                    {payment_status}
                </span>
            </div>
            <div class="muted" style="margin-top:6px;">
                更新：{escape(updated_at)}
            </div>
        </div>
        """

    return rows, total_limit_sum, available_limit_sum


def ensure_jinjia_month_statuses(month: str) -> None:
    response = (
        supabase
        .table("jinjia_statuses")
        .select("item_type,item_name")
        .eq("month", month)
        .execute()
    )

    existing = {
        (str(item.get("item_type")), str(item.get("item_name")))
        for item in (response.data or [])
    }

    missing = []

    for item_name in JINJIA_BILLS:
        if ("帳單", item_name) not in existing:
            missing.append(
                {
                    "month": month,
                    "item_type": "帳單",
                    "item_name": item_name,
                    "status": "未繳交",
                    "amount": 0,
                }
            )

    for item_name in JINJIA_PEOPLE:
        if ("人物", item_name) not in existing:
            missing.append(
                {
                    "month": month,
                    "item_type": "人物",
                    "item_name": item_name,
                    "status": "未繳交",
                    "amount": 0,
                }
            )

    if missing:
        supabase.table("jinjia_statuses").insert(missing).execute()


def get_jinjia_statuses(month: str) -> list[dict[str, Any]]:
    ensure_jinjia_month_statuses(month)

    response = (
        supabase
        .table("jinjia_statuses")
        .select("*")
        .eq("month", month)
        .order("item_type")
        .order("item_name")
        .execute()
    )
    return response.data or []


def update_jinjia_status(
    month: str,
    item_type: str,
    item_name: str,
    status: str,
    amount: int,
) -> None:
    (
        supabase
        .table("jinjia_statuses")
        .upsert(
            {
                "month": month,
                "item_type": item_type,
                "item_name": item_name,
                "status": status,
                "amount": amount,
                "updated_at": datetime.now(TAIPEI).isoformat(),
            },
            on_conflict="month,item_type,item_name",
        )
        .execute()
    )


def get_user_debts(user_id: str) -> list[dict[str, Any]]:
    response = (
        supabase
        .table("debts")
        .select("*")
        .eq("line_user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return response.data or []


def get_month_summary(user_id: str) -> tuple[float, float, float]:
    current_month = datetime.now(TAIPEI).strftime("%Y-%m")

    response = (
        supabase
        .table("transactions")
        .select("type,amount,created_at")
        .eq("line_user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )

    monthly_income = 0.0
    monthly_expense = 0.0

    for item in response.data or []:
        created_at = str(item.get("created_at", ""))
        if not created_at.startswith(current_month):
            continue

        amount = float(item.get("amount") or 0)
        if item.get("type") == "收入":
            monthly_income += amount
        elif item.get("type") == "支出":
            monthly_expense += amount

    return (
        monthly_income,
        monthly_expense,
        monthly_income - monthly_expense,
    )


# ----------------------------
# Dashboard
# ----------------------------

@app.route("/", methods=["GET"])
def home():
    try:
        transaction_response = (
            supabase
            .table("transactions")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        )
        all_transactions = transaction_response.data or []
        transactions = [
            item for item in all_transactions
            if normalize_account(item.get("account")) == "個人"
        ]
        jinjia_transactions = [
            item for item in all_transactions
            if normalize_account(item.get("account")) == "金家水電"
        ]
    except Exception as error:
        print("Dashboard 讀取 transactions 失敗：", error)
        all_transactions = []
        transactions = []
        jinjia_transactions = []

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
        print("Dashboard 讀取 debts 失敗：", error)
        debts = []

    total_debt = sum(
        float(item.get("remaining_amount") or 0)
        for item in debts
    )

    current_month = datetime.now(TAIPEI).strftime("%Y-%m")

    try:
        personal_bank_balances = get_bank_balances("個人")
    except Exception as error:
        print("讀取個人銀行餘額失敗：", error)
        personal_bank_balances = {
            bank: {"balance": 0, "updated_at": None}
            for bank in PERSONAL_BANKS
        }

    try:
        jinjia_bank_balances = get_bank_balances("金家")
    except Exception as error:
        print("讀取金家銀行餘額失敗：", error)
        jinjia_bank_balances = {
            bank: {"balance": 0, "updated_at": None}
            for bank in JINJIA_BANKS
        }

    personal_current_balance = sum(
        int(item.get("balance") or 0)
        for item in personal_bank_balances.values()
    )
    jinjia_current_balance = sum(
        int(item.get("balance") or 0)
        for item in jinjia_bank_balances.values()
    )

    total_assets = personal_current_balance + jinjia_current_balance
    net_worth = total_assets - total_debt

    personal_bank_rows = build_bank_balance_rows(
        personal_bank_balances,
        personal_current_balance,
        "personal-bank-bar",
    )
    jinjia_bank_rows = build_bank_balance_rows(
        jinjia_bank_balances,
        jinjia_current_balance,
        "jinjia-bank-bar",
    )

    try:
        credit_cards = get_credit_cards()
    except Exception as error:
        print("讀取信用卡額度失敗：", error)
        credit_cards = {
            card: {
                "total_limit": 0,
                "available_limit": 0,
                "statement_day": 0,
                "due_day": 0,
                "statement_amount": 0,
                "payment_status": "未繳交",
                "updated_at": None,
            }
            for card in CREDIT_CARDS
        }

    (
        credit_card_rows,
        total_credit_limit,
        total_available_credit,
    ) = build_credit_card_rows(credit_cards)

    total_credit_percent = (
        total_available_credit / total_credit_limit * 100
        if total_credit_limit > 0
        else 0
    )

    try:
        jinjia_statuses = get_jinjia_statuses(current_month)
    except Exception as error:
        print("讀取金家繳交狀態失敗：", error)
        jinjia_statuses = []

    monthly_income = 0.0
    monthly_expense = 0.0

    for item in transactions:
        created_at = str(item.get("created_at", ""))
        if not created_at.startswith(current_month):
            continue

        amount = float(item.get("amount") or 0)
        if item.get("type") == "收入":
            monthly_income += amount
        elif item.get("type") == "支出":
            monthly_expense += amount

    monthly_balance = monthly_income - monthly_expense

    expense_by_category: dict[str, float] = {}
    income_by_category: dict[str, float] = {}

    for item in transactions:
        created_at = str(item.get("created_at", ""))
        if not created_at.startswith(current_month):
            continue

        category = str(item.get("category") or "未分類")
        amount = float(item.get("amount") or 0)

        if item.get("type") == "支出":
            expense_by_category[category] = (
                expense_by_category.get(category, 0) + amount
            )
        elif item.get("type") == "收入":
            income_by_category[category] = (
                income_by_category.get(category, 0) + amount
            )

    expense_summary_rows = ""
    for category, amount in sorted(
        expense_by_category.items(),
        key=lambda pair: pair[1],
        reverse=True,
    ):
        percent = (
            amount / monthly_expense * 100
            if monthly_expense > 0
            else 0
        )
        expense_summary_rows += f"""
        <div class="summary-item">
            <div class="summary-row">
                <span>{escape(category)}</span>
                <strong>NT$ {amount:,.0f}</strong>
            </div>
            <div class="progress">
                <div class="progress-bar expense-bar"
                     style="width:{percent:.1f}%"></div>
            </div>
            <div class="muted">{percent:.0f}%</div>
        </div>
        """

    if not expense_summary_rows:
        expense_summary_rows = '<div class="muted">本月尚無支出資料。</div>'

    salary_income = float(income_by_category.get("薪水", 0))
    bonus_income = float(income_by_category.get("獎金", 0))
    other_income = sum(
        amount
        for category, amount in income_by_category.items()
        if category not in {"薪水", "獎金"}
    )

    other_income_details = []
    for category, amount in sorted(
        income_by_category.items(),
        key=lambda pair: pair[1],
        reverse=True,
    ):
        if category in {"薪水", "獎金"}:
            continue
        other_income_details.append(
            f"{escape(category)} NT$ {amount:,.0f}"
        )

    other_income_note = (
        "、".join(other_income_details)
        if other_income_details
        else "尚無其他收入"
    )

    jinjia_income = 0.0
    jinjia_expense = 0.0

    for item in jinjia_transactions:
        created_at = str(item.get("created_at", ""))
        if not created_at.startswith(current_month):
            continue

        amount = float(item.get("amount") or 0)

        if item.get("type") == "收入":
            jinjia_income += amount
        elif item.get("type") == "支出":
            jinjia_expense += amount

    jinjia_balance = jinjia_income - jinjia_expense

    bill_status_cards = ""
    person_status_cards = ""

    for item in jinjia_statuses:
        item_name = escape(str(item.get("item_name") or "未命名"))
        status = str(item.get("status") or "未繳交")
        amount = int(item.get("amount") or 0)
        status_class = "paid" if status == "已繳交" else "unpaid"
        amount_text = (
            f'<div class="status-amount">NT$ {amount:,}</div>'
            if amount > 0
            else ""
        )

        card = f"""
        <div class="status-card">
            <div>
                <strong>{item_name}</strong>
                {amount_text}
            </div>
            <span class="payment-status {status_class}">{status}</span>
        </div>
        """

        if item.get("item_type") == "帳單":
            bill_status_cards += card
        elif item.get("item_type") == "人物":
            person_status_cards += card

    if not bill_status_cards:
        bill_status_cards = '<div class="muted">尚無帳單狀態資料。</div>'

    if not person_status_cards:
        person_status_cards = '<div class="muted">尚無人物繳交資料。</div>'

    jinjia_recent_rows = ""
    for item in jinjia_transactions[:10]:
        created_at = str(item.get("created_at", ""))[:10]
        description = escape(str(item.get("description") or "未填寫"))
        transaction_type = str(item.get("type") or "")
        amount = float(item.get("amount") or 0)
        sign = "+" if transaction_type == "收入" else "-"
        css_class = "income" if transaction_type == "收入" else "expense"

        jinjia_recent_rows += f"""
        <tr>
            <td>{created_at}</td>
            <td>{description}</td>
            <td>{transaction_type}</td>
            <td class="{css_class}">{sign} NT$ {amount:,.0f}</td>
        </tr>
        """

    if not jinjia_recent_rows:
        jinjia_recent_rows = """
        <tr>
            <td>尚無資料</td>
            <td>請從 LINE 輸入金家收入或支出</td>
            <td>—</td>
            <td>NT$ 0</td>
        </tr>
        """

    debt_records = [
        {
            "created_at": debt.get("created_at", ""),
            "type": "負債",
            "category": debt.get("debt_type") or "其他",
            "description": debt.get("debt_name") or "未填寫",
            "amount": debt.get("remaining_amount") or 0,
        }
        for debt in debts
    ]

    recent_items = transactions + debt_records
    recent_items.sort(
        key=lambda item: str(item.get("created_at", "")),
        reverse=True,
    )

    recent_rows = ""
    for item in recent_items[:12]:
        created_at = str(item.get("created_at", ""))[:10]
        description = escape(str(item.get("description") or "未填寫"))
        category = escape(str(item.get("category") or "未分類"))
        transaction_type = str(item.get("type") or "")
        amount = float(item.get("amount") or 0)

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

    debt_cards = ""
    for debt in debts:
        debt_name = escape(str(debt.get("debt_name") or "未命名負債"))
        debt_type = escape(str(debt.get("debt_type") or "其他"))
        original = float(debt.get("original_amount") or 0)
        remaining = float(debt.get("remaining_amount") or 0)
        paid = max(original - remaining, 0)
        progress = (paid / original * 100) if original > 0 else 0
        progress = min(max(progress, 0), 100)

        debt_cards += f"""
        <div class="debt-item">
            <div class="debt-row">
                <div>
                    <strong>{debt_name}</strong>
                    <div class="muted">{debt_type}</div>
                </div>
                <div class="debt-amount">
                    NT$ {remaining:,.0f}
                </div>
            </div>
            <div class="progress">
                <div class="progress-bar" style="width: {progress:.1f}%"></div>
            </div>
            <div class="muted">
                已還 {progress:.0f}% · 原始 NT$ {original:,.0f}
            </div>
        </div>
        """

    if not debt_cards:
        debt_cards = '<div class="muted">尚未建立負債資料。</div>'

    top_expense_category = None
    top_expense_amount = 0.0
    if expense_by_category:
        top_expense_category, top_expense_amount = max(
            expense_by_category.items(),
            key=lambda pair: pair[1],
        )

    if monthly_income == 0 and monthly_expense == 0:
        ai_advice = "目前尚未取得本月收支資料，請先從 LINE 輸入記帳內容。"
    elif monthly_expense > monthly_income:
        ai_advice = (
            "本月支出目前高於收入，建議先檢查非必要支出，"
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
        top_text = (
            f"本月花費最高為「{top_expense_category}」"
            f" NT$ {top_expense_amount:,.0f}。"
            if top_expense_category
            else ""
        )
        ai_advice = (
            f"本月目前結餘 NT$ {monthly_balance:,.0f}，"
            f"結餘率約 {savings_rate:.0f}%。"
            f"{top_text}"
            "建議保留一部分作為緊急預備金。"
        )

    html = f"""
    <!DOCTYPE html>
    <html lang="zh-Hant">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>AI 財務管家</title>
        <style>
            :root {{
                --bg: #f5f5f7;
                --surface: rgba(255,255,255,.88);
                --surface-solid: #ffffff;
                --text: #1d1d1f;
                --muted: #6e6e73;
                --line: rgba(0,0,0,.08);
                --shadow: 0 18px 50px rgba(0,0,0,.08);
                --blue: #007aff;
                --green: #30b45a;
                --red: #ff3b30;
                --orange: #ff9500;
                --purple: #7d5cff;
                --radius-xl: 28px;
                --radius-lg: 20px;
            }}
            html[data-theme="dark"] {{
                --bg: #0b0b0d;
                --surface: rgba(28,28,30,.88);
                --surface-solid: #1c1c1e;
                --text: #f5f5f7;
                --muted: #a1a1a6;
                --line: rgba(255,255,255,.10);
                --shadow: 0 20px 60px rgba(0,0,0,.45);
            }}
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            html {{ scroll-behavior: smooth; }}
            body {{
                font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display",
                    "Segoe UI", "Microsoft JhengHei", sans-serif;
                background:
                    radial-gradient(circle at 8% 0%, rgba(0,122,255,.13), transparent 32%),
                    radial-gradient(circle at 92% 10%, rgba(125,92,255,.12), transparent 30%),
                    var(--bg);
                color: var(--text);
                min-height: 100vh;
                transition: background .25s, color .25s;
            }}
            .topbar {{
                position: sticky; top: 0; z-index: 20;
                backdrop-filter: blur(22px);
                background: color-mix(in srgb, var(--bg) 78%, transparent);
                border-bottom: 1px solid var(--line);
            }}
            .topbar-inner {{
                width: min(1180px, 92%); margin: auto; min-height: 72px;
                display: flex; align-items: center; justify-content: space-between; gap: 16px;
            }}
            .brand {{ display: flex; align-items: center; gap: 12px; font-weight: 760; }}
            .brand-icon {{
                width: 42px; height: 42px; border-radius: 14px; display: grid; place-items: center;
                color: white; background: linear-gradient(145deg, #111827, #007aff);
                box-shadow: 0 10px 24px rgba(0,122,255,.25);
            }}
            .theme-toggle {{
                border: 1px solid var(--line); background: var(--surface); color: var(--text);
                width: 44px; height: 44px; border-radius: 50%; cursor: pointer; font-size: 18px;
            }}
            .hero {{
                width: min(1180px, 92%); margin: 28px auto 0; border-radius: 34px;
                overflow: hidden; position: relative; min-height: 245px; padding: 34px; color: white;
                background: radial-gradient(circle at 75% 15%, rgba(255,255,255,.30), transparent 22%),
                    linear-gradient(135deg, #111827 0%, #0a5bd8 55%, #7d5cff 100%);
                box-shadow: 0 28px 70px rgba(0,80,180,.24);
            }}
            .hero::after {{
                content: ""; position: absolute; width: 280px; height: 280px; right: -85px;
                bottom: -145px; border: 1px solid rgba(255,255,255,.30); border-radius: 50%;
                box-shadow: 0 0 0 34px rgba(255,255,255,.05), 0 0 0 70px rgba(255,255,255,.04);
            }}
            .hero-kicker {{ opacity: .78; font-size: 14px; margin-bottom: 12px; }}
            .hero h1 {{ font-size: clamp(30px, 5vw, 54px); letter-spacing: -.045em; line-height: 1.02; margin-bottom: 10px; }}
            .hero-value {{ font-size: clamp(28px, 4.5vw, 48px); font-weight: 780; letter-spacing: -.04em; margin-top: 26px; }}
            .hero-sub {{ margin-top: 8px; opacity: .84; }}
            .container {{ width: min(1180px, 92%); margin: 0 auto; padding-bottom: 46px; }}
            .summary {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; margin-top: 20px; }}
            .card, .section {{ background: var(--surface); border: 1px solid var(--line); backdrop-filter: blur(20px); box-shadow: var(--shadow); }}
            .card {{
                padding: 22px; border-radius: var(--radius-lg); min-height: 126px; position: relative;
                overflow: hidden; transition: transform .2s ease, box-shadow .2s ease;
            }}
            .card:hover, .bank-balance-item:hover, .status-card:hover {{ transform: translateY(-3px); }}
            .card-title, .muted {{ color: var(--muted); font-size: 14px; }}
            .card-title {{ margin-bottom: 12px; font-weight: 650; }}
            .amount {{ font-size: clamp(23px, 3vw, 30px); font-weight: 770; letter-spacing: -.035em; }}
            .income {{ color: var(--green); }} .expense {{ color: var(--red); }}
            .balance {{ color: var(--blue); }} .debt {{ color: var(--orange); }}
            .current-balance {{ color: var(--purple); }}
            .section {{ margin-top: 22px; padding: 26px; border-radius: var(--radius-xl); }}
            .section h2 {{ font-size: 23px; letter-spacing: -.025em; margin-bottom: 18px; }}
            .account-header {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 18px; }}
            .account-header h2 {{ margin-bottom: 0; }}
            .account-badge {{
                background: rgba(0,122,255,.10); color: var(--blue); padding: 7px 11px;
                border-radius: 999px; font-size: 13px; font-weight: 700;
            }}
            .bank-balance-list {{ display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 14px; }}
            .bank-balance-item, .income-card, .status-card, .debt-item {{
                border: 1px solid var(--line); border-radius: var(--radius-lg); padding: 18px;
                background: color-mix(in srgb, var(--surface-solid) 92%, transparent);
                transition: transform .2s ease, border-color .2s ease;
            }}
            .credit-card-item {{ position: relative; overflow: hidden; min-height: 190px; }}
            .credit-card-item::before {{
                content: "●●"; letter-spacing: 6px; position: absolute; right: 18px; top: 17px; opacity: .14; font-size: 18px;
            }}
            .credit-safe {{ border-color: rgba(48,180,90,.25); }}
            .credit-warning {{ border-color: rgba(255,149,0,.50); }}
            .credit-danger {{ border-color: rgba(255,59,48,.62); box-shadow: 0 12px 34px rgba(255,59,48,.11); }}
            .progress {{
                height: 9px; background: color-mix(in srgb, var(--muted) 18%, transparent);
                border-radius: 999px; overflow: hidden; margin: 10px 0 8px;
            }}
            .progress-bar {{ height: 100%; border-radius: inherit; background: var(--blue); }}
            .personal-bank-bar {{ background: linear-gradient(90deg, #007aff, #61a8ff); }}
            .jinjia-bank-bar {{ background: linear-gradient(90deg, #ff9500, #ffd166); }}
            .credit-card-bar {{ background: linear-gradient(90deg, #7d5cff, #c08cff); }}
            .expense-bar {{ background: linear-gradient(90deg, #ff3b30, #ff8a80); }}
            .summary-row, .debt-row {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; }}
            .summary-item {{ padding: 13px 0; border-bottom: 1px solid var(--line); }}
            .summary-item:last-child {{ border-bottom: 0; }}
            .income-grid, .status-grid, .debt-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 14px; }}
            .income-card strong {{ display: block; margin-top: 8px; font-size: 24px; color: var(--green); }}
            .status-card {{ display: flex; justify-content: space-between; align-items: center; gap: 10px; }}
            .payment-status {{ padding: 6px 10px; border-radius: 999px; font-size: 13px; white-space: nowrap; font-weight: 750; }}
            .payment-status.paid {{ background: rgba(48,180,90,.13); color: var(--green); }}
            .payment-status.unpaid {{ background: rgba(255,59,48,.13); color: var(--red); }}
            .status-amount {{ color: var(--muted); font-size: 13px; margin-top: 5px; }}
            .debt-grid {{ grid-template-columns: repeat(2, minmax(0,1fr)); }}
            .debt-amount {{ color: var(--orange); font-weight: 750; white-space: nowrap; }}
            .ai-box {{
                background: linear-gradient(135deg, rgba(0,122,255,.10), rgba(125,92,255,.10));
                border: 1px solid rgba(0,122,255,.18); padding: 20px; border-radius: var(--radius-lg); line-height: 1.9;
            }}
            .status {{
                display: inline-flex; align-items: center; gap: 8px; background: rgba(48,180,90,.13);
                color: var(--green); padding: 8px 12px; border-radius: 999px; font-size: 14px; font-weight: 700;
            }}
            table {{ width: 100%; border-collapse: collapse; }}
            th, td {{ padding: 14px 12px; text-align: left; border-bottom: 1px solid var(--line); }}
            th {{ color: var(--muted); font-size: 13px; font-weight: 700; text-transform: uppercase; }}
            .table-wrap {{ overflow-x: auto; border-radius: 14px; }}
            .footer {{ text-align: center; color: var(--muted); padding: 30px; font-size: 13px; }}
            @media (max-width: 900px) {{
                .summary {{ grid-template-columns: repeat(2, minmax(0,1fr)); }}
                .income-grid, .status-grid {{ grid-template-columns: repeat(2, minmax(0,1fr)); }}
            }}
            @media (max-width: 640px) {{
                .hero {{ padding: 28px 22px; min-height: 225px; border-radius: 26px; }}
                .summary, .bank-balance-list, .income-grid, .status-grid, .debt-grid {{ grid-template-columns: 1fr; }}
                .section {{ padding: 20px; border-radius: 22px; }}
                .card {{ min-height: 112px; }}
                .topbar-inner {{ min-height: 64px; }}
                th, td {{ padding: 11px 8px; font-size: 13px; }}
            }}
        </style>
    </head>
    <body>
        <div class="topbar">
            <div class="topbar-inner">
                <div class="brand">
                    <div class="brand-icon">¥</div>
                    <span>AI 財務管家</span>
                </div>
                <button class="theme-toggle" id="themeToggle"
                        aria-label="切換深色模式">◐</button>
            </div>
        </div>

        <section class="hero">
            <div class="hero-kicker">個人財務總覽 · {current_month}</div>
            <h1>掌握每一筆錢，<br>讓財務更有方向。</h1>
            <div class="hero-value">NT$ {net_worth:,.0f}</div>
            <div class="hero-sub">目前淨資產</div>
        </section>

        <div class="container">
            <div class="summary">
                <div class="card">
                    <div class="card-title">個人銀行總餘額</div>
                    <div class="amount current-balance">
                        NT$ {personal_current_balance:,.0f}
                    </div>
                </div>
                <div class="card">
                    <div class="card-title">本月收入</div>
                    <div class="amount income">NT$ {monthly_income:,.0f}</div>
                </div>
                <div class="card">
                    <div class="card-title">本月支出</div>
                    <div class="amount expense">NT$ {monthly_expense:,.0f}</div>
                </div>
                <div class="card">
                    <div class="card-title">本月結餘</div>
                    <div class="amount balance">NT$ {monthly_balance:,.0f}</div>
                </div>
                <div class="card">
                    <div class="card-title">總負債</div>
                    <div class="amount debt">NT$ {total_debt:,.0f}</div>
                </div>
            </div>

            <div class="section">
                <h2>資產與淨資產總覽</h2>
                <div class="summary">
                    <div class="card">
                        <div class="card-title">個人銀行總餘額</div>
                        <div class="amount income">
                            NT$ {personal_current_balance:,.0f}
                        </div>
                    </div>
                    <div class="card">
                        <div class="card-title">金家王道銀行</div>
                        <div class="amount income">
                            NT$ {jinjia_current_balance:,.0f}
                        </div>
                    </div>
                    <div class="card">
                        <div class="card-title">總資產</div>
                        <div class="amount current-balance">
                            NT$ {total_assets:,.0f}
                        </div>
                    </div>
                    <div class="card">
                        <div class="card-title">總負債</div>
                        <div class="amount debt">
                            NT$ {total_debt:,.0f}
                        </div>
                    </div>
                    <div class="card">
                        <div class="card-title">淨資產</div>
                        <div class="amount balance">
                            NT$ {net_worth:,.0f}
                        </div>
                    </div>
                </div>
            </div>

            <div class="section">
                <h2>個人銀行帳戶餘額</h2>
                <div class="bank-balance-list">{personal_bank_rows}</div>
                <div class="summary-row" style="margin-top:16px;">
                    <strong>個人總餘額</strong>
                    <strong>NT$ {personal_current_balance:,.0f}</strong>
                </div>
            </div>

            <div class="section">
                <h2>本月花費管理</h2>
                {expense_summary_rows}
            </div>

            <div class="section">
                <h2>本月收入管理</h2>
                <div class="income-grid">
                    <div class="income-card">
                        <div class="muted">薪水</div>
                        <strong>NT$ {salary_income:,.0f}</strong>
                    </div>
                    <div class="income-card">
                        <div class="muted">獎金</div>
                        <strong>NT$ {bonus_income:,.0f}</strong>
                    </div>
                    <div class="income-card">
                        <div class="muted">其他收入</div>
                        <strong>NT$ {other_income:,.0f}</strong>
                        <div class="muted" style="margin-top:8px;">
                            {other_income_note}
                        </div>
                    </div>
                </div>
            </div>

            <div class="section">
                <div class="account-header">
                    <h2>信用卡可用額度</h2>
                    <span class="account-badge">獨立顯示</span>
                </div>

                <div class="bank-balance-list">{credit_card_rows}</div>

                <div class="summary" style="margin-top:18px;">
                    <div class="card">
                        <div class="card-title">總信用額度</div>
                        <div class="amount">NT$ {total_credit_limit:,.0f}</div>
                    </div>
                    <div class="card">
                        <div class="card-title">總可用額度</div>
                        <div class="amount current-balance">
                            NT$ {total_available_credit:,.0f}
                        </div>
                    </div>
                    <div class="card">
                        <div class="card-title">整體可用比例</div>
                        <div class="amount">{total_credit_percent:.1f}%</div>
                    </div>
                </div>
            </div>

            <div class="section">
                <div class="account-header">
                    <h2>金家水電帳戶</h2>
                    <span class="account-badge">獨立帳戶</span>
                </div>

                <div class="summary" style="margin-top:0;">
                    <div class="card">
                        <div class="card-title">王道銀行餘額</div>
                        <div class="amount current-balance">
                            NT$ {jinjia_current_balance:,.0f}
                        </div>
                    </div>
                    <div class="card">
                        <div class="card-title">本月收入</div>
                        <div class="amount income">NT$ {jinjia_income:,.0f}</div>
                    </div>
                    <div class="card">
                        <div class="card-title">本月支出</div>
                        <div class="amount expense">NT$ {jinjia_expense:,.0f}</div>
                    </div>
                    <div class="card">
                        <div class="card-title">本月結餘</div>
                        <div class="amount balance">NT$ {jinjia_balance:,.0f}</div>
                    </div>
                </div>

                <h3 style="margin:22px 0 12px;">王道銀行帳戶比例</h3>
                <div class="bank-balance-list">{jinjia_bank_rows}</div>

                <h3 style="margin:22px 0 12px;">帳單繳交狀態</h3>
                <div class="status-grid">{bill_status_cards}</div>

                <h3 style="margin:22px 0 12px;">人物繳交狀態</h3>
                <div class="status-grid">{person_status_cards}</div>

                <h3 style="margin:22px 0 12px;">最近紀錄</h3>
                <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>日期</th>
                            <th>項目</th>
                            <th>類型</th>
                            <th>金額</th>
                        </tr>
                    </thead>
                    <tbody>{jinjia_recent_rows}</tbody>
                </table>
                </div>
            </div>

            <div class="section">
                <h2>負債管理</h2>
                <div class="debt-grid">{debt_cards}</div>
            </div>

            <div class="section">
                <h2>最近記帳紀錄</h2>
                <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>日期</th>
                            <th>項目</th>
                            <th>分類</th>
                            <th>金額</th>
                        </tr>
                    </thead>
                    <tbody>{recent_rows}</tbody>
                </table>
                </div>
            </div>

            <div class="section">
                <h2>LINE Bot 狀態</h2>
                <span class="status">系統運作中</span>
                <p style="margin-top:12px;color:#6b7280;line-height:1.8;">
                    支出：早餐 85<br>
                    收入：薪水 70000<br>
                    負債：負債 玉山信用卡 40000<br>
                    還款：還款 玉山信用卡 3000<br>
                    查詢：本月、花費查詢、收入查詢、負債查詢、幫助<br>
                    歷史：歷史 2026-07、歷史 支出 2026-07、
                    歷史 收入 2026-07、歷史 飲食、歷史 薪水、
                    歷史 信用卡刷卡、歷史 負債<br>
                    金家歷史：金家歷史、金家歷史 收入、
                    金家歷史 支出 2026-07
                </p>
            </div>

            <div class="section">
                <h2>AI 財務建議</h2>
                <div class="ai-box">{ai_advice}</div>
            </div>
        </div>

        <div class="footer">
            AI Finance Manager · Powered by LINE Bot
        </div>
        <script>
            const root = document.documentElement;
            const button = document.getElementById("themeToggle");
            const saved = localStorage.getItem("finance-theme");
            if (saved) root.dataset.theme = saved;
            button.addEventListener("click", () => {{
                const next = root.dataset.theme === "dark" ? "light" : "dark";
                root.dataset.theme = next;
                localStorage.setItem("finance-theme", next);
            }});
        </script>
    </body>
    </html>
    """

    return html


def run_system_health_checks() -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}

    table_checks = {
        "transactions": "id,line_user_id,account,type,category,amount,description,created_at",
        "debts": "id,line_user_id,debt_name,remaining_amount,created_at",
        "bank_balances": "owner,bank_name,balance,updated_at",
        "bank_balance_history": "owner,bank_name,balance,recorded_at",
        "credit_cards": "card_name,total_limit,available_limit,updated_at",
        "jinjia_payment_status": "month,item_type,item_name,status,amount",
    }

    for table_name, columns in table_checks.items():
        try:
            (
                supabase
                .table(table_name)
                .select(columns)
                .limit(1)
                .execute()
            )
            checks[table_name] = {"ok": True}
        except Exception as error:
            checks[table_name] = {
                "ok": False,
                "error": str(error)[:300],
            }

    all_ok = all(item["ok"] for item in checks.values())
    return {
        "status": "ok" if all_ok else "degraded",
        "checked_at": datetime.now(TAIPEI).isoformat(),
        "checks": checks,
    }


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200


@app.route("/health/full", methods=["GET"])
def full_health():
    report = run_system_health_checks()
    return report, 200 if report["status"] == "ok" else 503


# ----------------------------
# LINE Webhook
# ----------------------------

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


@handler.add(
    MessageEvent,
    message=TextMessageContent,
)
def handle_message(event: MessageEvent):
    user_text = event.message.text.strip()
    user_id = event.source.user_id

    if user_text in {"系統檢查", "健康檢查", "系統狀態"}:
        report = run_system_health_checks()
        lines = [
            "🩺 AI 財務管家健康檢查",
            f"整體狀態：{'正常' if report['status'] == 'ok' else '部分異常'}",
        ]

        labels = {
            "transactions": "收支記帳",
            "debts": "負債",
            "bank_balances": "銀行餘額",
            "bank_balance_history": "銀行歷史",
            "credit_cards": "信用卡",
            "jinjia_payment_status": "金家繳交狀態",
        }

        for key, result in report["checks"].items():
            icon = "✅" if result["ok"] else "❌"
            lines.append(f"{icon} {labels.get(key, key)}")

        if report["status"] != "ok":
            lines.append("\n詳細錯誤請查看 Render Logs 或 /health/full")

        reply_line(event, "\n".join(lines))
        return

    # 幫助
    if user_text in {"幫助", "help", "Help", "HELP"}:
        reply_line(
            event,
            "📘 使用方式\n\n"
            "系統檢查：系統檢查\n"
            "支出：早餐 85\n"
            "刷卡：刷卡 電影 500\n"
            "收入：薪水 70000\n"
            "金家支出：金家支出 水費 3000\n"
            "金家收入：金家收入 15000\n"
            "銀行餘額：設定玉山銀行 100000\n"
            "銀行餘額：設定中國信託 50000\n"
            "銀行餘額：設定渣打銀行 30000\n"
            "銀行餘額：設定華南銀行 20000\n"
            "銀行餘額：設定LINE Bank 15000\n"
            "銀行餘額：設定LINE Pay Money 5000\n"
            "金家餘額：設定王道銀行 80000\n"
            "信用卡總額度：設定玉山信用卡額度 100000\n"
            "信用卡可用額度：設定玉山信用卡可用額度 65000\n"
            "信用卡查詢：信用卡額度\n"
            "信用卡結帳日：設定玉山信用卡結帳日 5\n"
            "信用卡繳款日：設定玉山信用卡繳款日 20\n"
            "信用卡應繳：設定玉山信用卡應繳 12500\n"
            "信用卡狀態：玉山信用卡 已繳交\n"
            "銀行歷史：銀行歷史 玉山銀行\n"
            "帳單狀態：水費 已繳交 650\n"
            "人物狀態：俊億 未繳交 3500\n"
            "狀態查詢：金家狀態\n"
            "負債：負債 玉山信用卡 40000\n"
            "還款：還款 玉山信用卡 3000\n"
            "查詢：本月\n"
            "查詢：花費查詢\n"
            "查詢：收入查詢\n"
            "查詢：負債查詢\n"
            "歷史：歷史 2026-07\n"
            "歷史：歷史 支出 2026-07\n"
            "歷史：歷史 收入 2026-07\n"
            "歷史：歷史 負債",
        )
        return

    # 信用卡額度設定與查詢
    credit_total_match = re.match(
        r"^設定\s*(玉山信用卡|中信信用卡|兆豐信用卡)額度\s+(\d[\d,]*)\s*$",
        user_text,
    )

    if credit_total_match:
        card_name = credit_total_match.group(1)
        total_limit = int(
            credit_total_match.group(2).replace(",", "")
        )

        try:
            current = get_credit_cards().get(
                card_name,
                {"available_limit": 0},
            )
            available_limit = min(
                int(current.get("available_limit") or 0),
                total_limit,
            )
            set_credit_card_values(
                card_name,
                total_limit=total_limit,
                available_limit=available_limit,
            )
        except Exception as error:
            print("設定信用卡總額度失敗：", error)
            reply_line(event, f"設定失敗：{error}")
            return

        reply_line(
            event,
            f"✅ 已設定{card_name}總額度\n"
            f"NT$ {total_limit:,}",
        )
        return

    credit_available_match = re.match(
        r"^設定\s*(玉山信用卡|中信信用卡|兆豐信用卡)可用額度\s+(\d[\d,]*)\s*$",
        user_text,
    )

    if credit_available_match:
        card_name = credit_available_match.group(1)
        available_limit = int(
            credit_available_match.group(2).replace(",", "")
        )

        try:
            set_credit_card_values(
                card_name,
                available_limit=available_limit,
            )
        except Exception as error:
            print("設定信用卡可用額度失敗：", error)
            reply_line(event, f"設定失敗：{error}")
            return

        reply_line(
            event,
            f"✅ 已設定{card_name}可用額度\n"
            f"NT$ {available_limit:,}",
        )
        return

    credit_statement_day_match = re.match(
        r"^設定\s*(玉山信用卡|中信信用卡|兆豐信用卡)結帳日\s+(\d{1,2})\s*$",
        user_text,
    )

    if credit_statement_day_match:
        card_name = credit_statement_day_match.group(1)
        day = int(credit_statement_day_match.group(2))

        try:
            set_credit_card_values(card_name, statement_day=day)
        except Exception as error:
            reply_line(event, f"設定失敗：{error}")
            return

        reply_line(event, f"✅ 已設定{card_name}結帳日：每月 {day} 日")
        return

    credit_due_day_match = re.match(
        r"^設定\s*(玉山信用卡|中信信用卡|兆豐信用卡)繳款日\s+(\d{1,2})\s*$",
        user_text,
    )

    if credit_due_day_match:
        card_name = credit_due_day_match.group(1)
        day = int(credit_due_day_match.group(2))

        try:
            set_credit_card_values(card_name, due_day=day)
        except Exception as error:
            reply_line(event, f"設定失敗：{error}")
            return

        reply_line(event, f"✅ 已設定{card_name}繳款日：每月 {day} 日")
        return

    credit_statement_amount_match = re.match(
        r"^設定\s*(玉山信用卡|中信信用卡|兆豐信用卡)應繳\s+(\d[\d,]*)\s*$",
        user_text,
    )

    if credit_statement_amount_match:
        card_name = credit_statement_amount_match.group(1)
        amount = int(
            credit_statement_amount_match.group(2).replace(",", "")
        )

        try:
            set_credit_card_values(
                card_name,
                statement_amount=amount,
                payment_status="未繳交",
            )
        except Exception as error:
            reply_line(event, f"設定失敗：{error}")
            return

        reply_line(
            event,
            f"✅ 已設定{card_name}本期應繳\nNT$ {amount:,}\n狀態：未繳交",
        )
        return

    credit_payment_status_match = re.match(
        r"^(玉山信用卡|中信信用卡|兆豐信用卡)\s*(已繳交|未繳交|已繳|未繳)\s*$",
        user_text,
    )

    if credit_payment_status_match:
        card_name = credit_payment_status_match.group(1)
        raw_status = credit_payment_status_match.group(2)
        status = "已繳交" if raw_status in {"已繳交", "已繳"} else "未繳交"

        try:
            set_credit_card_values(card_name, payment_status=status)
        except Exception as error:
            reply_line(event, f"更新失敗：{error}")
            return

        reply_line(event, f"✅ {card_name}狀態已更新為：{status}")
        return

    if user_text in {"信用卡額度", "可用額度", "信用卡查詢"}:
        try:
            cards = get_credit_cards()
        except Exception as error:
            print("信用卡額度查詢失敗：", error)
            reply_line(event, "信用卡額度查詢失敗，請稍後再試。")
            return

        lines = ["💳 信用卡可用額度"]
        total_limit_sum = 0
        available_limit_sum = 0

        for card_name, values in cards.items():
            total_limit = int(values.get("total_limit") or 0)
            available_limit = int(values.get("available_limit") or 0)
            percent = (
                available_limit / total_limit * 100
                if total_limit > 0
                else 0
            )

            total_limit_sum += total_limit
            available_limit_sum += available_limit

            statement_day = int(values.get("statement_day") or 0)
            due_day = int(values.get("due_day") or 0)
            statement_amount = int(
                values.get("statement_amount") or 0
            )
            payment_status = str(
                values.get("payment_status") or "未繳交"
            )
            updated_at = format_taipei_datetime(
                values.get("updated_at")
            )

            lines.append(
                f"{card_name}\n"
                f"可用：NT$ {available_limit:,}\n"
                f"比例：{percent:.1f}%\n"
                f"本期應繳：NT$ {statement_amount:,}\n"
                f"結帳日：{statement_day or '未設定'}\n"
                f"繳款日：{due_day or '未設定'}\n"
                f"狀態：{payment_status}\n"
                f"更新：{updated_at}"
            )

        total_percent = (
            available_limit_sum / total_limit_sum * 100
            if total_limit_sum > 0
            else 0
        )
        lines.append(
            f"\n總額度：NT$ {total_limit_sum:,}\n"
            f"總可用：NT$ {available_limit_sum:,}\n"
            f"整體比例：{total_percent:.1f}%"
        )

        reply_line(event, "\n\n".join(lines))
        return

    # 銀行帳戶餘額設定與查詢
    bank_balance_match = re.match(
        r"^設定\s*(玉山(?:銀行)?|中信(?:銀行)?|中國信託(?:銀行)?|"
        r"渣打(?:銀行)?|華南(?:銀行)?|LINE\s*Bank|"
        r"LINE\s*Pay\s*Money|王道(?:銀行)?)"
        r"\s+(-?\d[\d,]*)\s*$",
        user_text,
        re.IGNORECASE,
    )

    if bank_balance_match:
        raw_bank_name = bank_balance_match.group(1)
        bank_name = normalize_bank_name(raw_bank_name)

        if not bank_name:
            reply_line(event, "無法辨識銀行名稱，請輸入「幫助」查看格式。")
            return
        owner = "金家" if bank_name == "王道銀行" else "個人"

        try:
            target_balance = int(
                bank_balance_match.group(2).replace(",", "")
            )
            set_bank_balance(owner, bank_name, target_balance)
        except Exception as error:
            print("設定銀行餘額失敗：", error)
            reply_line(event, "設定銀行餘額失敗（BANK-SET），請稍後再試。")
            return

        reply_line(
            event,
            f"✅ 已設定{bank_name}餘額\n"
            f"NT$ {target_balance:,}",
        )
        return

    if user_text in {"餘額查詢", "帳戶餘額", "銀行餘額"}:
        try:
            personal_balances = get_bank_balances("個人")
            jinjia_balances = get_bank_balances("金家")
        except Exception as error:
            print("銀行餘額查詢失敗：", error)
            reply_line(event, "銀行餘額查詢失敗，請稍後再試。")
            return

        personal_total = sum(
            int(item.get("balance") or 0)
            for item in personal_balances.values()
        )
        jinjia_total = sum(
            int(item.get("balance") or 0)
            for item in jinjia_balances.values()
        )

        lines = ["💰 個人銀行帳戶"]
        for bank_name, values in personal_balances.items():
            balance = int(values.get("balance") or 0)
            updated_at = format_taipei_datetime(
                values.get("updated_at")
            )
            percent = balance / personal_total * 100 if personal_total > 0 else 0
            lines.append(
                f"{bank_name}：NT$ {balance:,}（{percent:.1f}%）\n"
                f"更新：{updated_at}"
            )

        lines.append(f"個人總餘額：NT$ {personal_total:,}")
        lines.append("")
        lines.append("🏠 金家銀行帳戶")

        for bank_name, values in jinjia_balances.items():
            balance = int(values.get("balance") or 0)
            updated_at = format_taipei_datetime(
                values.get("updated_at")
            )
            percent = balance / jinjia_total * 100 if jinjia_total > 0 else 0
            lines.append(
                f"{bank_name}：NT$ {balance:,}（{percent:.1f}%）\n"
                f"更新：{updated_at}"
            )

        lines.append(f"金家總餘額：NT$ {jinjia_total:,}")
        reply_line(event, "\n".join(lines))
        return

    bank_history_match = re.match(
        r"^銀行歷史(?:\s+(玉山銀行|中國信託|渣打銀行|華南銀行|LINE Bank|LINE Pay Money|王道銀行))?$",
        user_text,
        re.IGNORECASE,
    )

    if bank_history_match:
        raw_bank_name = bank_history_match.group(1)
        normalized_names = {
            "line bank": "LINE Bank",
            "line pay money": "LINE Pay Money",
        }
        bank_name = (
            normalized_names.get(raw_bank_name.lower(), raw_bank_name)
            if raw_bank_name
            else None
        )

        try:
            records = get_bank_balance_history(bank_name, 20)
        except Exception as error:
            print("銀行餘額歷史查詢失敗：", error)
            reply_line(event, "銀行餘額歷史查詢失敗，請稍後再試。")
            return

        if not records:
            reply_line(event, "查無銀行餘額歷史紀錄。")
            return

        lines = [f"📈 銀行餘額歷史｜{bank_name or '全部銀行'}"]
        for item in records:
            recorded_at = format_taipei_datetime(
                item.get("recorded_at")
            )
            lines.append(
                f"{item.get('bank_name')}｜"
                f"NT$ {int(item.get('balance') or 0):,}｜"
                f"{recorded_at}"
            )

        reply_line(event, "\n".join(lines))
        return

    # 金家帳單 / 人物繳交狀態
    jinjia_status_match = re.match(
        r"^(水費|電費|網路費|俊億|宗暉|俊宏)\s+"
        r"(已繳交|未繳交|已繳|未繳)"
        r"(?:\s+(\d[\d,]*))?\s*$",
        user_text,
    )

    if jinjia_status_match:
        item_name = jinjia_status_match.group(1)
        raw_status = jinjia_status_match.group(2)
        status = "已繳交" if raw_status in {"已繳交", "已繳"} else "未繳交"
        amount = parse_positive_int(
            jinjia_status_match.group(3) or "0"
        ) or 0
        item_type = "帳單" if item_name in JINJIA_BILLS else "人物"
        month = datetime.now(TAIPEI).strftime("%Y-%m")

        try:
            update_jinjia_status(
                month,
                item_type,
                item_name,
                status,
                amount,
            )
        except Exception as error:
            print("更新金家繳交狀態失敗：", error)
            reply_line(event, "更新繳交狀態失敗，請稍後再試。")
            return

        amount_text = f"\n金額：NT$ {amount:,}" if amount > 0 else ""
        reply_line(
            event,
            f"🏠 已更新金家繳交狀態\n"
            f"項目：{item_name}\n"
            f"狀態：{status}"
            f"{amount_text}",
        )
        return

    if user_text in {"金家狀態", "繳交狀態"}:
        month = datetime.now(TAIPEI).strftime("%Y-%m")

        try:
            statuses = get_jinjia_statuses(month)
        except Exception as error:
            print("查詢金家狀態失敗：", error)
            reply_line(event, "金家狀態查詢失敗，請稍後再試。")
            return

        lines = [f"🏠 金家繳交狀態｜{month}"]
        for item in statuses:
            amount = int(item.get("amount") or 0)
            amount_text = f"｜NT$ {amount:,}" if amount > 0 else ""
            lines.append(
                f"{item.get('item_name')}｜"
                f"{item.get('status') or '未繳交'}"
                f"{amount_text}"
            )

        reply_line(event, "\n".join(lines))
        return

    status_history_match = re.match(
        r"^金家狀態歷史\s+(\d{4}-\d{2})$",
        user_text,
    )

    if status_history_match:
        month = status_history_match.group(1)

        try:
            statuses = get_jinjia_statuses(month)
        except Exception as error:
            print("查詢金家狀態歷史失敗：", error)
            reply_line(event, "金家狀態歷史查詢失敗，請稍後再試。")
            return

        lines = [f"🏠 金家繳交狀態歷史｜{month}"]
        for item in statuses:
            amount = int(item.get("amount") or 0)
            amount_text = f"｜NT$ {amount:,}" if amount > 0 else ""
            lines.append(
                f"{item.get('item_name')}｜"
                f"{item.get('status') or '未繳交'}"
                f"{amount_text}"
            )

        reply_line(event, "\n".join(lines))
        return

    # 金家水電帳戶：支出 / 收入
    jinjia_match = re.match(
        r"^金家(支出|收入)\s+(?:(.+?)\s+)?(\d[\d,]*)\s*$",
        user_text,
    )

    if jinjia_match:
        entry_type = jinjia_match.group(1)
        description = (jinjia_match.group(2) or entry_type).strip()
        amount = parse_positive_int(jinjia_match.group(3))

        if amount is None:
            reply_line(event, "金額必須是大於 0 的整數。")
            return

        category = (
            "收入"
            if entry_type == "收入"
            else classify_expense(description)
        )

        try:
            supabase.table("transactions").insert(
                {
                    "line_user_id": user_id,
                    "account": "金家水電",
                    "type": entry_type,
                    "category": category,
                    "amount": amount,
                    "description": description,
                }
            ).execute()
        except Exception as error:
            print("金家水電記帳失敗：", error)
            reply_line(event, "金家水電記帳失敗（JINJIA-TX），請先執行健康檢查 SQL。")
            return

        sign = "+" if entry_type == "收入" else "-"
        reply_line(
            event,
            f"🏠 金家水電已記錄{entry_type}\n"
            f"分類：{category}\n"
            f"項目：{description}\n"
            f"金額：{sign} NT$ {amount:,}",
        )
        return

    if user_text.startswith(("金家支出", "金家收入")):
        reply_line(
            event,
            "格式錯誤\n"
            "支出：金家支出 水費 3000\n"
            "收入：金家收入 15000",
        )
        return

    if user_text in {"金家本月", "金家收支", "金家查詢"}:
        try:
            records = get_account_transactions("金家水電", user_id)
        except Exception as error:
            print("金家水電本月查詢失敗：", error)
            reply_line(event, "金家水電資料查詢失敗，請稍後再試。")
            return

        current_month = datetime.now(TAIPEI).strftime("%Y-%m")
        income = 0
        expense = 0

        for item in records:
            if not str(item.get("created_at", "")).startswith(current_month):
                continue
            amount = int(item.get("amount") or 0)
            if item.get("type") == "收入":
                income += amount
            elif item.get("type") == "支出":
                expense += amount

        reply_line(
            event,
            f"🏠 金家水電本月收支\n"
            f"收入：NT$ {income:,}\n"
            f"支出：NT$ {expense:,}\n"
            f"結餘：NT$ {income - expense:,}",
        )
        return

    jinjia_history_match = re.match(
        r"^金家歷史(?:\s+(收入|支出))?(?:\s+(\d{4}-\d{2}))?$",
        user_text,
    )

    if jinjia_history_match:
        jinjia_type = jinjia_history_match.group(1)
        month = jinjia_history_match.group(2)

        try:
            records = get_account_transactions("金家水電", user_id)
        except Exception as error:
            print("金家歷史查詢失敗：", error)
            reply_line(event, "金家歷史查詢失敗，請稍後再試。")
            return

        if month:
            records = [
                item for item in records
                if str(item.get("created_at", "")).startswith(month)
            ]

        if jinjia_type:
            records = [
                item for item in records
                if item.get("type") == jinjia_type
            ]

        if not records:
            reply_line(event, "查無金家水電歷史紀錄。")
            return

        history_title = jinjia_type or "全部收支"
        lines = [f"🏠 金家水電歷史｜{history_title}｜{month or '全部月份'}"]
        for item in records[:20]:
            date_text = str(item.get("created_at", ""))[:10]
            sign = "+" if item.get("type") == "收入" else "-"
            lines.append(
                f"{date_text}｜{item.get('description') or '未填寫'}"
                f"｜{item.get('category') or '未分類'}"
                f"｜{sign}NT$ {int(item.get('amount') or 0):,}"
            )

        reply_line(event, "\n".join(lines))
        return

    # 本月收支查詢
    if user_text in {"本月", "本月收支", "收支查詢"}:
        try:
            income, expense, balance = get_month_summary(user_id)
        except Exception as error:
            print("本月查詢失敗：", error)
            reply_line(event, "本月資料查詢失敗，請稍後再試。")
            return

        reply_line(
            event,
            f"📊 本月收支\n"
            f"收入：NT$ {income:,.0f}\n"
            f"支出：NT$ {expense:,.0f}\n"
            f"結餘：NT$ {balance:,.0f}",
        )
        return

    # 歷史紀錄查詢
    # 支援：
    # 歷史 2026-07
    # 歷史 支出 2026-07
    # 歷史 收入 2026-07
    # 歷史 負債
    history_match = re.match(
        r"^歷史(?:\s+([^\s]+))?(?:\s+(\d{4}-\d{2}))?$",
        user_text,
    )

    if history_match:
        history_filter = history_match.group(1)
        history_month = history_match.group(2)

        if history_filter == "負債":
            try:
                debts = get_user_debts(user_id)
            except Exception as error:
                print("負債歷史查詢失敗：", error)
                reply_line(event, "負債歷史查詢失敗，請稍後再試。")
                return

            if history_month:
                debts = [
                    debt
                    for debt in debts
                    if str(debt.get("created_at", "")).startswith(history_month)
                ]

            if not debts:
                reply_line(event, "查無負債歷史紀錄。")
                return

            lines = ["📚 負債歷史紀錄"]
            for debt in debts[:20]:
                created_at = str(debt.get("created_at", ""))[:10]
                lines.append(
                    f"{created_at}｜{debt.get('debt_name') or '未命名'}"
                    f"｜原始 NT$ {int(debt.get('original_amount') or 0):,}"
                    f"｜剩餘 NT$ {int(debt.get('remaining_amount') or 0):,}"
                )
            reply_line(event, "\n".join(lines))
            return

        try:
            query = (
                supabase
                .table("transactions")
                .select("*")
                .eq("line_user_id", user_id)
                .order("created_at", desc=True)
            )

            if history_filter in {"收入", "支出"}:
                query = query.eq("type", history_filter)

            response = query.execute()
            records = response.data or []
        except Exception as error:
            print("收支歷史查詢失敗：", error)
            reply_line(event, "歷史紀錄查詢失敗，請稍後再試。")
            return

        if history_month:
            records = [
                item
                for item in records
                if str(item.get("created_at", "")).startswith(history_month)
            ]

        if history_filter and history_filter not in {"收入", "支出"}:
            records = [
                item
                for item in records
                if str(item.get("category") or "") == history_filter
            ]

        if not records:
            reply_line(event, "查無符合條件的歷史紀錄。")
            return

        title = "全部收支" if not history_filter else history_filter
        month_text = history_month or "全部月份"
        lines = [f"📚 {title}歷史｜{month_text}"]

        for item in records[:20]:
            created_at = str(item.get("created_at", ""))[:10]
            sign = "+" if item.get("type") == "收入" else "-"
            lines.append(
                f"{created_at}｜{item.get('description') or '未填寫'}"
                f"｜{item.get('category') or '未分類'}"
                f"｜{sign}NT$ {int(item.get('amount') or 0):,}"
            )

        if len(records) > 20:
            lines.append(f"\n共 {len(records)} 筆，目前顯示最新 20 筆。")

        reply_line(event, "\n".join(lines))
        return

    # 花費分類查詢
    if user_text in {"花費查詢", "支出分類", "花在哪"}:
        try:
            current_month = datetime.now(TAIPEI).strftime("%Y-%m")
            response = (
                supabase
                .table("transactions")
                .select("category,amount,created_at")
                .eq("line_user_id", user_id)
                .eq("type", "支出")
                .order("created_at", desc=True)
                .execute()
            )
        except Exception as error:
            print("花費查詢失敗：", error)
            reply_line(event, "花費查詢失敗，請稍後再試。")
            return

        totals: dict[str, int] = {}
        for item in response.data or []:
            if not str(item.get("created_at", "")).startswith(current_month):
                continue
            category = str(item.get("category") or "未分類")
            totals[category] = totals.get(category, 0) + int(
                item.get("amount") or 0
            )

        if not totals:
            reply_line(event, "本月尚無支出資料。")
            return

        total_expense = sum(totals.values())
        lines = ["🧾 本月花費分類"]
        for category, amount in sorted(
            totals.items(),
            key=lambda pair: pair[1],
            reverse=True,
        ):
            percent = amount / total_expense * 100 if total_expense else 0
            lines.append(f"{category}：NT$ {amount:,}（{percent:.0f}%）")
        lines.append(f"\n總支出：NT$ {total_expense:,}")
        reply_line(event, "\n".join(lines))
        return

    # 收入分類查詢
    if user_text in {"收入查詢", "薪水查詢", "收入分類"}:
        try:
            current_month = datetime.now(TAIPEI).strftime("%Y-%m")
            response = (
                supabase
                .table("transactions")
                .select("category,amount,created_at")
                .eq("line_user_id", user_id)
                .eq("type", "收入")
                .order("created_at", desc=True)
                .execute()
            )
        except Exception as error:
            print("收入查詢失敗：", error)
            reply_line(event, "收入查詢失敗，請稍後再試。")
            return

        totals: dict[str, int] = {}
        for item in response.data or []:
            if not str(item.get("created_at", "")).startswith(current_month):
                continue
            category = str(item.get("category") or "其他收入")
            totals[category] = totals.get(category, 0) + int(
                item.get("amount") or 0
            )

        if not totals:
            reply_line(event, "本月尚無收入資料。")
            return

        total_income = sum(totals.values())
        lines = ["💵 本月收入分類"]
        for category, amount in sorted(
            totals.items(),
            key=lambda pair: pair[1],
            reverse=True,
        ):
            percent = amount / total_income * 100 if total_income else 0
            lines.append(f"{category}：NT$ {amount:,}（{percent:.0f}%）")
        lines.append(f"\n總收入：NT$ {total_income:,}")
        reply_line(event, "\n".join(lines))
        return

    # 負債查詢
    if user_text in {"負債查詢", "我的負債", "總負債"}:
        try:
            debts = get_user_debts(user_id)
        except Exception as error:
            print("負債查詢失敗：", error)
            reply_line(event, "負債查詢失敗，請稍後再試。")
            return

        active_debts = [
            debt for debt in debts
            if int(debt.get("remaining_amount") or 0) > 0
        ]

        if not active_debts:
            reply_line(event, "目前沒有未清償負債。")
            return

        total = sum(
            int(debt.get("remaining_amount") or 0)
            for debt in active_debts
        )

        lines = ["💳 目前負債"]
        for debt in active_debts[:10]:
            lines.append(
                f"{debt.get('debt_name') or '未命名'}："
                f"NT$ {int(debt.get('remaining_amount') or 0):,}"
            )
        lines.append(f"\n總負債：NT$ {total:,}")

        reply_line(event, "\n".join(lines))
        return

    # 還款
    repayment_match = re.match(
        r"^還款\s+(.+?)\s+(\d[\d,]*)\s*$",
        user_text,
    )

    if repayment_match:
        debt_name = repayment_match.group(1).strip()
        payment_amount = parse_positive_int(repayment_match.group(2))

        if payment_amount is None:
            reply_line(event, "還款金額必須是大於 0 的整數。")
            return

        try:
            debt_response = (
                supabase
                .table("debts")
                .select("*")
                .eq("line_user_id", user_id)
                .ilike("debt_name", debt_name)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            matched_debts = debt_response.data or []

            if not matched_debts:
                debt_response = (
                    supabase
                    .table("debts")
                    .select("*")
                    .eq("line_user_id", user_id)
                    .ilike("debt_name", f"%{debt_name}%")
                    .order("created_at", desc=True)
                    .limit(1)
                    .execute()
                )
                matched_debts = debt_response.data or []

            if not matched_debts:
                reply_line(
                    event,
                    f"找不到負債：{debt_name}\n"
                    "請先輸入「負債 名稱 金額」。",
                )
                return

            debt = matched_debts[0]
            debt_id = debt.get("id")
            display_name = debt.get("debt_name") or debt_name
            current_remaining = int(debt.get("remaining_amount") or 0)

            if current_remaining <= 0:
                reply_line(event, f"{display_name} 已經還清。")
                return

            actual_payment = min(payment_amount, current_remaining)
            new_remaining = current_remaining - actual_payment

            # 先更新負債，再寫入支出；若支出寫入失敗就回復負債。
            supabase.table("debts").update(
                {
                    "remaining_amount": new_remaining,
                    "monthly_payment": actual_payment,
                }
            ).eq("id", debt_id).execute()

            try:
                supabase.table("transactions").insert(
                    {
                        "line_user_id": user_id,
                        "account": "個人",
                        "type": "支出",
                        "category": "貸款",
                        "amount": actual_payment,
                        "description": f"還款 {display_name}",
                    }
                ).execute()
            except Exception:
                supabase.table("debts").update(
                    {
                        "remaining_amount": current_remaining,
                        "monthly_payment": int(
                            debt.get("monthly_payment") or 0
                        ),
                    }
                ).eq("id", debt_id).execute()
                raise

        except Exception as error:
            print("還款處理失敗：", error)
            reply_line(event, "還款失敗，系統已記錄錯誤，請稍後再試。")
            return

        extra_note = ""
        if payment_amount > current_remaining:
            extra_note = "\n輸入金額超過剩餘負債，已自動以剩餘金額結清。"

        reply_line(
            event,
            f"💰 已記錄還款\n"
            f"名稱：{display_name}\n"
            f"還款金額：NT$ {actual_payment:,}\n"
            f"剩餘負債：NT$ {new_remaining:,}"
            f"{extra_note}",
        )
        return

    if user_text.startswith("還款"):
        reply_line(
            event,
            "格式錯誤\n請輸入：還款 名稱 金額\n"
            "例如：還款 玉山信用卡 3000",
        )
        return

    # 新增負債：固定使用「負債 名稱 金額」
    debt_match = re.match(
        r"^負債\s+(.+?)\s+(\d[\d,]*)\s*$",
        user_text,
    )

    if debt_match:
        debt_name = debt_match.group(1).strip()
        amount = parse_positive_int(debt_match.group(2))

        if amount is None:
            reply_line(event, "負債金額必須是大於 0 的整數。")
            return

        try:
            supabase.table("debts").insert(
                {
                    "line_user_id": user_id,
                    "debt_name": debt_name,
                    "debt_type": classify_debt(debt_name),
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
            f"類型：{classify_debt(debt_name)}\n"
            f"剩餘金額：NT$ {amount:,}",
        )
        return

    if user_text.startswith("負債"):
        reply_line(
            event,
            "格式錯誤\n請輸入：負債 名稱 金額\n"
            "例如：負債 玉山信用卡 40000",
        )
        return

    # 信用卡刷卡快捷指令
    credit_match = re.match(
        r"^(?:刷卡|信用卡)\s+(.+?)\s+(\d[\d,]*)\s*$",
        user_text,
    )

    if credit_match:
        description = credit_match.group(1).strip()
        amount = parse_positive_int(credit_match.group(2))

        if amount is None:
            reply_line(event, "刷卡金額必須是大於 0 的整數。")
            return

        try:
            supabase.table("transactions").insert(
                {
                    "line_user_id": user_id,
                    "account": "個人",
                    "type": "支出",
                    "category": "信用卡刷卡",
                    "amount": amount,
                    "description": description,
                }
            ).execute()
        except Exception as error:
            print("信用卡刷卡記帳失敗：", error)
            reply_line(event, "刷卡記帳失敗，請稍後再試。")
            return

        reply_line(
            event,
            f"💳 已記錄信用卡刷卡\n"
            f"項目：{description}\n"
            f"金額：- NT$ {amount:,}",
        )
        return

    if user_text.startswith(("刷卡", "信用卡")):
        reply_line(
            event,
            "格式錯誤\n請輸入：刷卡 項目 金額\n"
            "例如：刷卡 電影 500",
        )
        return

    # 一般收支
    transaction = parse_transaction(user_text)

    if transaction is None:
        reply_line(
            event,
            "我看不懂這筆記帳。\n\n"
            "請輸入像這樣：\n"
            "早餐 85\n"
            "加油 500\n"
            "薪水 70000\n"
            "負債 玉山信用卡 40000\n"
            "還款 玉山信用卡 3000\n"
            "或輸入「幫助」。",
        )
        return

    try:
        supabase.table("transactions").insert(
            {
                "line_user_id": user_id,
                "account": "個人",
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
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
