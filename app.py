import os
import ast
import operator
import re
from datetime import datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, redirect, request, session, url_for
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
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-this-secret-key")

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

handler = WebhookHandler(LINE_CHANNEL_SECRET)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

TAIPEI = ZoneInfo("Asia/Taipei")
APP_VERSION = "4.5.0 Nebula Ultimate"


@app.before_request
def redirect_root_to_jarvis():
    """Use Project JARVIS as the public home page while keeping legacy routes intact."""
    if request.path == "/":
        return redirect(url_for("jarvis_garage"))
    return None


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


ALLOWED_MATH_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def safe_eval_number_expression(expression: str, current_value: int = 0) -> int:
    raw = (expression or "").replace(",", "").replace(" ", "")
    if not raw:
        raise ValueError("數字不可空白")
    if len(raw) > 60:
        raise ValueError("運算式過長")

    if raw[0] in "+-*/":
        raw = f"{current_value}{raw}"

    try:
        tree = ast.parse(raw, mode="eval")
    except SyntaxError as error:
        raise ValueError("運算格式錯誤") from error

    def evaluate(node):
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("只允許數字")
            return node.value
        if isinstance(node, ast.UnaryOp) and type(node.op) in ALLOWED_MATH_OPERATORS:
            return ALLOWED_MATH_OPERATORS[type(node.op)](evaluate(node.operand))
        if isinstance(node, ast.BinOp) and type(node.op) in ALLOWED_MATH_OPERATORS:
            left = evaluate(node.left)
            right = evaluate(node.right)
            if isinstance(node.op, ast.Div) and right == 0:
                raise ValueError("不可除以 0")
            return ALLOWED_MATH_OPERATORS[type(node.op)](left, right)
        raise ValueError("只允許 +、-、*、/ 與括號")

    result = evaluate(tree)
    if abs(result) > 1_000_000_000_000:
        raise ValueError("計算結果過大")
    if result < 0:
        raise ValueError("結果不可小於 0")
    return int(round(result))


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


def get_latest_transaction(
    user_id: str,
    account: str | None = None,
) -> dict[str, Any] | None:
    query = (
        supabase
        .table("transactions")
        .select("*")
        .eq("line_user_id", user_id)
        .order("created_at", desc=True)
        .limit(1)
    )

    if account:
        query = query.eq("account", account)

    response = query.execute()
    rows = response.data or []
    return rows[0] if rows else None


def update_transaction_record(
    transaction_id: Any,
    *,
    transaction_type: str | None = None,
    category: str | None = None,
    amount: int | None = None,
    description: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}

    if transaction_type is not None:
        if transaction_type not in {"收入", "支出"}:
            raise ValueError("類型只能是收入或支出")
        payload["type"] = transaction_type

    if category is not None:
        cleaned_category = category.strip()
        if not cleaned_category:
            raise ValueError("分類不可空白")
        payload["category"] = cleaned_category

    if amount is not None:
        if amount <= 0:
            raise ValueError("金額必須大於 0")
        payload["amount"] = amount

    if description is not None:
        cleaned_description = description.strip()
        if not cleaned_description:
            raise ValueError("項目不可空白")
        payload["description"] = cleaned_description

    if account is not None:
        normalized = normalize_account(account)
        if normalized not in {"個人", "金家水電"}:
            raise ValueError("帳戶只能是個人或金家水電")
        payload["account"] = normalized

    if not payload:
        raise ValueError("沒有可修改的內容")

    response = (
        supabase
        .table("transactions")
        .update(payload)
        .eq("id", transaction_id)
        .execute()
    )
    rows = response.data or []
    return rows[0] if rows else payload


def write_audit_log(
    *,
    action: str,
    source: str,
    entity_type: str,
    entity_id: str | int | None = None,
    before_data: dict[str, Any] | None = None,
    after_data: dict[str, Any] | None = None,
) -> None:
    try:
        (
            supabase
            .table("audit_logs")
            .insert(
                {
                    "action": action,
                    "source": source,
                    "entity_type": entity_type,
                    "entity_id": str(entity_id) if entity_id is not None else None,
                    "before_data": before_data,
                    "after_data": after_data,
                }
            )
            .execute()
        )
    except Exception as error:
        print("寫入 audit_logs 失敗：", error)


def delete_transaction_record(transaction_id: Any) -> None:
    (
        supabase
        .table("transactions")
        .delete()
        .eq("id", transaction_id)
        .execute()
    )


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
JINJIA_BILLS = ("網路費", "水費", "電費")
JINJIA_PEOPLE = ("俊宏", "俊億", "宗暉")
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
                <div class="account-header">
                    <h2>LINE 指令總表</h2>
                    <a class="account-badge" href="/admin"
                       style="text-decoration:none;">開啟網頁管理</a>
                </div>

                <h3 style="margin:8px 0 12px;">① 記帳與修改</h3>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>功能</th><th>格式</th><th>直接照著打</th></tr></thead>
                        <tbody>
                            <tr><td>個人支出</td><td>項目 金額</td><td><code>早餐 85</code></td></tr>
                            <tr><td>個人收入</td><td>收入項目 金額</td><td><code>薪水 55000</code></td></tr>
                            <tr><td>金家收入</td><td>金家收入 金額</td><td><code>金家收入 4500</code></td></tr>
                            <tr><td>金家支出</td><td>金家支出 項目 金額</td><td><code>金家支出 材料 3000</code></td></tr>
                            <tr><td>刷卡</td><td>刷卡 項目 金額</td><td><code>刷卡 電影 500</code></td></tr>
                            <tr><td>查看上一筆</td><td>查看上一筆</td><td><code>查看上一筆</code></td></tr>
                            <tr><td>修改全部</td><td>修改上一筆 項目 金額</td><td><code>修改上一筆 午餐 150</code></td></tr>
                            <tr><td>只改金額</td><td>修改上一筆金額 金額</td><td><code>修改上一筆金額 150</code></td></tr>
                            <tr><td>只改項目</td><td>修改上一筆項目 項目</td><td><code>修改上一筆項目 午餐</code></td></tr>
                            <tr><td>只改分類</td><td>修改上一筆分類 分類</td><td><code>修改上一筆分類 飲食</code></td></tr>
                            <tr><td>刪除上一筆</td><td>先刪除，再確認</td><td><code>刪除上一筆</code> → <code>確認刪除</code></td></tr>
                        </tbody>
                    </table>
                </div>

                <h3 style="margin:24px 0 12px;">② 銀行與信用卡</h3>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>功能</th><th>格式</th><th>直接照著打</th></tr></thead>
                        <tbody>
                            <tr><td>設定銀行餘額</td><td>設定 銀行 金額</td><td><code>設定 中國信託 215</code></td></tr>
                            <tr><td>查銀行餘額</td><td>帳戶餘額</td><td><code>帳戶餘額</code></td></tr>
                            <tr><td>銀行歷史</td><td>銀行歷史 銀行</td><td><code>銀行歷史 玉山銀行</code></td></tr>
                            <tr><td>卡片總額度</td><td>設定卡片額度 金額</td><td><code>設定玉山信用卡額度 100000</code></td></tr>
                            <tr><td>卡片可用額度</td><td>設定卡片可用額度 金額</td><td><code>設定玉山信用卡可用額度 65000</code></td></tr>
                            <tr><td>卡片應繳</td><td>設定卡片應繳 金額</td><td><code>設定玉山信用卡應繳 12500</code></td></tr>
                            <tr><td>標記已繳</td><td>卡片名稱 已繳交</td><td><code>玉山信用卡 已繳交</code></td></tr>
                            <tr><td>查信用卡</td><td>信用卡額度</td><td><code>信用卡額度</code></td></tr>
                        </tbody>
                    </table>
                </div>

                <h3 style="margin:24px 0 12px;">③ 金家固定帳單與人物</h3>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>固定項目</th><th>格式</th><th>直接照著打</th></tr></thead>
                        <tbody>
                            <tr><td>網路費</td><td>網路費 狀態 金額</td><td><code>網路費 已繳交 899</code></td></tr>
                            <tr><td>水費</td><td>水費 狀態 金額</td><td><code>水費 未繳交 650</code></td></tr>
                            <tr><td>電費</td><td>電費 狀態 金額</td><td><code>電費 已繳交 3500</code></td></tr>
                            <tr><td>俊宏</td><td>俊宏 狀態 金額</td><td><code>俊宏 已繳交 3000</code></td></tr>
                            <tr><td>俊億</td><td>俊億 狀態 金額</td><td><code>俊億 未繳交 3500</code></td></tr>
                            <tr><td>宗暉</td><td>宗暉 狀態 金額</td><td><code>宗暉 已繳交 2500</code></td></tr>
                        </tbody>
                    </table>
                </div>

                <h3 style="margin:24px 0 12px;">④ 負債與查詢</h3>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>功能</th><th>格式</th><th>直接照著打</th></tr></thead>
                        <tbody>
                            <tr><td>新增負債</td><td>負債 名稱 金額</td><td><code>負債 車貸 180000</code></td></tr>
                            <tr><td>還款</td><td>還款 名稱 金額</td><td><code>還款 車貸 5000</code></td></tr>
                            <tr><td>本月收支</td><td>本月</td><td><code>本月</code></td></tr>
                            <tr><td>金家本月</td><td>金家本月</td><td><code>金家本月</code></td></tr>
                            <tr><td>歷史查詢</td><td>歷史 類型 月份</td><td><code>歷史 支出 2026-07</code></td></tr>
                            <tr><td>系統檢查</td><td>系統檢查</td><td><code>系統檢查</code></td></tr>
                        </tbody>
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
            AI Finance Manager · Version {APP_VERSION} · Powered by LINE Bot
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
    checks["configuration"] = {
        "ok": bool(ADMIN_PASSWORD)
        and app.secret_key != "change-this-secret-key",
        "warnings": [
            message
            for condition, message in (
                (not ADMIN_PASSWORD, "尚未設定 ADMIN_PASSWORD"),
                (
                    app.secret_key == "change-this-secret-key",
                    "尚未設定 FLASK_SECRET_KEY",
                ),
            )
            if condition
        ],
    }
    all_ok = all(item.get("ok", False) for item in checks.values())
    return {
        "status": "ok" if all_ok else "degraded",
        "version": APP_VERSION,
        "checked_at": datetime.now(TAIPEI).isoformat(),
        "checks": checks,
    }


def admin_logged_in() -> bool:
    return bool(session.get("finance_admin"))


def admin_page(message: str = "") -> str:
    login_message = (
        f'<div class="notice">{escape(message)}</div>'
        if message
        else ""
    )

    if not admin_logged_in():
        return f"""
        <!doctype html>
        <html lang="zh-Hant">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width,initial-scale=1">
            <title>財務管理登入</title>
            <style>
                body {{font-family:-apple-system,"Microsoft JhengHei",sans-serif;
                background:#f5f5f7;margin:0;padding:24px;color:#1d1d1f}}
                .box {{max-width:420px;margin:10vh auto;background:white;padding:28px;
                border-radius:24px;box-shadow:0 18px 60px rgba(0,0,0,.1)}}
                input,button {{width:100%;padding:13px;border-radius:12px;
                border:1px solid #ddd;margin-top:12px;font-size:16px;box-sizing:border-box}}
                button {{background:#007aff;color:white;border:0;font-weight:700}}
                .notice {{background:#fff3cd;padding:12px;border-radius:10px;margin-bottom:12px}}
            </style>
        </head>
        <body>
            <div class="box">
                <h1>財務管理登入</h1>
                <p>請輸入管理密碼。</p>
                {login_message}
                <form method="post" action="/admin/login">
                    <input type="password" name="password" required placeholder="管理密碼">
                    <button type="submit">登入</button>
                </form>
            </div>
        </body>
        </html>
        """

    try:
        response = (
            supabase
            .table("transactions")
            .select("*")
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        )
        records = response.data or []
    except Exception as error:
        records = []
        message = f"讀取記帳資料失敗：{error}"

    rows = ""
    for item in records:
        transaction_id = escape(str(item.get("id") or ""))
        created_at = escape(format_taipei_datetime(item.get("created_at")))
        account = escape(normalize_account(item.get("account")))
        tx_type = escape(str(item.get("type") or ""))
        category = escape(str(item.get("category") or ""))
        description = escape(str(item.get("description") or ""))
        amount = int(item.get("amount") or 0)

        rows += f"""
        <tr>
            <td>{created_at}</td>
            <td>{account}</td>
            <td>{tx_type}</td>
            <td>{category}</td>
            <td>{description}</td>
            <td>NT$ {amount:,}</td>
            <td class="actions">
                <a href="/admin/transaction/{transaction_id}/edit">編輯</a>
                <form method="post" action="/admin/transaction/{transaction_id}/delete"
                      onsubmit="return confirm('確定刪除這筆記帳？')">
                    <button type="submit" class="danger">刪除</button>
                </form>
            </td>
        </tr>
        """

    if not rows:
        rows = '<tr><td colspan="7">目前沒有記帳資料。</td></tr>'

    try:
        personal_banks = get_bank_balances("個人")
        jinjia_banks = get_bank_balances("金家")
    except Exception:
        personal_banks = {
            name: {"balance": 0, "updated_at": None}
            for name in PERSONAL_BANKS
        }
        jinjia_banks = {
            name: {"balance": 0, "updated_at": None}
            for name in JINJIA_BANKS
        }

    bank_forms = ""
    for owner, balances in (
        ("個人", personal_banks),
        ("金家", jinjia_banks),
    ):
        for bank_name, values in balances.items():
            balance = int(values.get("balance") or 0)
            bank_forms += f"""
            <form method="post" action="/admin/bank/update" class="edit-row">
                <input type="hidden" name="owner" value="{escape(owner)}">
                <input type="hidden" name="bank_name" value="{escape(bank_name)}">
                <strong>{escape(bank_name)}</strong>
                <input type="text" class="math-input" name="balance" value="{balance}" required>
                <button type="submit">儲存</button>
            </form>
            """

    try:
        cards = get_credit_cards()
    except Exception:
        cards = {
            name: {
                "total_limit": 0,
                "available_limit": 0,
                "statement_day": 0,
                "due_day": 0,
                "statement_amount": 0,
                "payment_status": "未繳交",
            }
            for name in CREDIT_CARDS
        }

    credit_forms = ""
    for card_name, values in cards.items():
        status = str(values.get("payment_status") or "未繳交")
        credit_forms += f"""
        <form method="post" action="/admin/credit-card/update" class="credit-form">
            <input type="hidden" name="card_name" value="{escape(card_name)}">
            <h3>{escape(card_name)}</h3>
            <label>總額度<input type="text" class="math-input" name="total_limit"
                value="{int(values.get('total_limit') or 0)}" required></label>
            <label>可用額度<input type="text" class="math-input" name="available_limit"
                value="{int(values.get('available_limit') or 0)}" required></label>
            <label>結帳日<input type="number" min="1" max="31" name="statement_day"
                value="{int(values.get('statement_day') or 0) or ''}"></label>
            <label>繳款日<input type="number" min="1" max="31" name="due_day"
                value="{int(values.get('due_day') or 0) or ''}"></label>
            <label>本期應繳<input type="text" class="math-input" name="statement_amount"
                value="{int(values.get('statement_amount') or 0)}" required></label>
            <label>狀態
                <select name="payment_status">
                    <option value="未繳交" {'selected' if status == '未繳交' else ''}>未繳交</option>
                    <option value="已繳交" {'selected' if status == '已繳交' else ''}>已繳交</option>
                </select>
            </label>
            <button type="submit">儲存信用卡資料</button>
        </form>
        """

    current_month = datetime.now(TAIPEI).strftime("%Y-%m")
    try:
        jinjia_items = get_jinjia_statuses(current_month)
    except Exception:
        jinjia_items = []

    jinjia_bill_forms = ""
    jinjia_people_forms = ""

    jinjia_lookup = {
        (str(item.get("item_type") or ""), str(item.get("item_name") or "")): item
        for item in jinjia_items
    }

    for item_name in JINJIA_BILLS:
        item = jinjia_lookup.get(
            ("帳單", item_name),
            {"status": "未繳交", "amount": 0},
        )
        status = str(item.get("status") or "未繳交")
        amount = int(item.get("amount") or 0)

        jinjia_bill_forms += f"""
        <form method="post" action="/admin/jinjia-status/update"
              class="jinjia-choice-card">
            <input type="hidden" name="month" value="{escape(current_month)}">
            <input type="hidden" name="item_type" value="帳單">
            <input type="hidden" name="item_name" value="{escape(item_name)}">
            <div class="choice-title">🧾 {escape(item_name)}</div>
            <label>繳交狀態
                <select name="status">
                    <option value="未繳交" {'selected' if status == '未繳交' else ''}>未繳交</option>
                    <option value="已繳交" {'selected' if status == '已繳交' else ''}>已繳交</option>
                </select>
            </label>
            <label>金額
                <input type="text" class="math-input" name="amount" value="{amount}" required>
            </label>
            <button type="submit">儲存帳單狀態</button>
        </form>
        """

    for item_name in JINJIA_PEOPLE:
        item = jinjia_lookup.get(
            ("人物", item_name),
            {"status": "未繳交", "amount": 0},
        )
        status = str(item.get("status") or "未繳交")
        amount = int(item.get("amount") or 0)

        jinjia_people_forms += f"""
        <form method="post" action="/admin/jinjia-status/update"
              class="jinjia-choice-card">
            <input type="hidden" name="month" value="{escape(current_month)}">
            <input type="hidden" name="item_type" value="人物">
            <input type="hidden" name="item_name" value="{escape(item_name)}">
            <div class="choice-title">👤 {escape(item_name)}</div>
            <label>繳交狀態
                <select name="status">
                    <option value="未繳交" {'selected' if status == '未繳交' else ''}>未繳交</option>
                    <option value="已繳交" {'selected' if status == '已繳交' else ''}>已繳交</option>
                </select>
            </label>
            <label>金額
                <input type="text" class="math-input" name="amount" value="{amount}" required>
            </label>
            <button type="submit">儲存人物狀態</button>
        </form>
        """

    try:
        debt_response = (
            supabase
            .table("debts")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        )
        admin_debts = debt_response.data or []
    except Exception:
        admin_debts = []

    debt_rows = ""
    for debt in admin_debts:
        debt_id = escape(str(debt.get("id") or ""))
        debt_rows += f"""
        <form method="post" action="/admin/debt/{debt_id}/update" class="debt-form">
            <input name="debt_name" value="{escape(str(debt.get('debt_name') or ''))}" required>
            <input name="debt_type" value="{escape(str(debt.get('debt_type') or '其他'))}" required>
            <input type="text" class="math-input" name="original_amount"
                value="{int(debt.get('original_amount') or 0)}" required>
            <input type="text" class="math-input" name="remaining_amount"
                value="{int(debt.get('remaining_amount') or 0)}" required>
            <input type="text" class="math-input" name="monthly_payment"
                value="{int(debt.get('monthly_payment') or 0)}" required>
            <button type="submit">儲存</button>
            <button type="submit" class="danger"
                formaction="/admin/debt/{debt_id}/delete"
                onclick="return confirm('確定刪除這筆負債？')">刪除</button>
        </form>
        """

    if not debt_rows:
        debt_rows = '<p>目前沒有負債資料。</p>'

    notice = f'<div class="notice">{escape(message)}</div>' if message else ""

    return f"""
    <!doctype html>
    <html lang="zh-Hant">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>網頁記帳管理</title>
        <style>
            body {{font-family:-apple-system,"Microsoft JhengHei",sans-serif;
            background:#f5f5f7;margin:0;color:#1d1d1f}}
            .wrap {{width:min(1180px,94%);margin:30px auto}}
            .card {{background:white;padding:22px;border-radius:22px;
            box-shadow:0 14px 45px rgba(0,0,0,.08);margin-bottom:20px}}
            .grid {{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}}
            input,select,button {{padding:11px;border-radius:10px;border:1px solid #ddd;
            font-size:15px;box-sizing:border-box}}
            button,.button {{background:#007aff;color:white;border:0;font-weight:700;
            text-decoration:none;display:inline-block;padding:11px 14px;border-radius:10px}}
            .danger {{background:#ff3b30;padding:8px 10px}}
            table {{width:100%;border-collapse:collapse}}
            th,td {{padding:11px;border-bottom:1px solid #eee;text-align:left}}
            .table-wrap {{overflow:auto}}
            .actions {{display:flex;gap:8px;align-items:center}}
            .actions form {{margin:0}}
            .notice {{background:#e8f3ff;padding:12px;border-radius:12px;margin-bottom:14px}}
            .top {{display:flex;justify-content:space-between;align-items:center;gap:12px}}
            .edit-row {{display:grid;grid-template-columns:2fr 1fr auto;gap:10px;
            align-items:center;padding:10px 0;border-bottom:1px solid #eee}}
            .credit-grid {{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}
            .credit-form {{border:1px solid #eee;padding:16px;border-radius:16px}}
            .credit-form label {{display:block;font-size:13px;margin-top:8px}}
            .credit-form input,.credit-form select {{width:100%;margin-top:4px}}
            .debt-form {{display:grid;grid-template-columns:2fr 1fr 1fr 1fr 1fr auto auto;
            gap:8px;padding:10px 0;border-bottom:1px solid #eee}}
            .status-edit {{grid-template-columns:2fr 1fr 1fr auto}}
            .jinjia-grid {{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}
            .jinjia-choice-card {{border:1px solid #e5e7eb;border-radius:16px;
            padding:16px;background:#fafafa}}
            .jinjia-choice-card label {{display:block;font-size:13px;margin-top:10px}}
            .jinjia-choice-card input,.jinjia-choice-card select {{
            width:100%;margin-top:5px}}
            .choice-title {{font-size:18px;font-weight:800}}
            .help-text {{color:#6b7280;margin:-6px 0 16px;line-height:1.7}}
            .quick-math {{display:flex;gap:5px;flex-wrap:wrap;margin-top:6px}}
            .quick-math button {{padding:5px 8px;background:#eef5ff;color:#007aff;
            border:1px solid #d8e8ff;font-size:12px}}
            .math-hint {{display:block;color:#6b7280;margin-top:4px;line-height:1.5}}
            .section-nav {{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0 20px}}
            .section-nav a {{text-decoration:none;background:#eef5ff;color:#007aff;
            padding:8px 10px;border-radius:10px;font-weight:700}}
            @media(max-width:900px) {{
                .credit-grid,.jinjia-grid {{grid-template-columns:1fr}}
                .debt-form {{grid-template-columns:1fr 1fr}}
            }}
            @media(max-width:760px) {{
                .grid {{grid-template-columns:1fr}}
                th,td {{font-size:13px;white-space:nowrap}}
            }}
        </style>
    </head>
    <body>
        <div class="wrap">
            <div class="top">
                <h1>網頁記帳管理</h1>
                <div>
                    <a class="button" href="/">回首頁</a>
                    <a class="button" href="/admin/logout">登出</a>
                </div>
            </div>
            {notice}
            <div class="card">
                <h2>新增記帳</h2>
                <form method="post" action="/admin/transaction/add" class="grid">
                    <select name="account">
                        <option value="個人">個人</option>
                        <option value="金家水電">金家水電</option>
                    </select>
                    <select name="type">
                        <option value="支出">支出</option>
                        <option value="收入">收入</option>
                    </select>
                    <input name="description" required placeholder="項目，例如早餐">
                    <input name="category" placeholder="分類，可留白自動判斷">
                    <input name="amount" required type="number" min="1" placeholder="金額">
                    <button type="submit">新增</button>
                </form>
            </div>
            <div class="section-nav">
                <a href="#banks">銀行</a>
                <a href="#cards">信用卡</a>
                <a href="#jinjia">金家狀態</a>
                <a href="#debts">負債</a>
                <a href="#transactions">記帳</a>
            </div>

            <div class="card" id="banks">
                <h2>銀行餘額管理</h2>
                {bank_forms}
            </div>

            <div class="card" id="cards">
                <h2>信用卡完整管理</h2>
                <div class="credit-grid">{credit_forms}</div>
            </div>

            <div class="card" id="jinjia">
                <h2>金家帳單與人物繳交管理（{current_month}）</h2>
                <p class="help-text">
                    每個固定項目都能直接選擇「已繳交」或「未繳交」，
                    並輸入金額後儲存。
                </p>

                <h3>固定帳單</h3>
                <div class="jinjia-grid">{jinjia_bill_forms}</div>

                <h3 style="margin-top:22px;">固定人物</h3>
                <div class="jinjia-grid">{jinjia_people_forms}</div>
            </div>

            <div class="card" id="debts">
                <h2>負債完整管理</h2>
                <form method="post" action="/admin/debt/add" class="debt-form">
                    <input name="debt_name" required placeholder="負債名稱">
                    <input name="debt_type" required placeholder="類型">
                    <input type="text" class="math-input" name="original_amount" required placeholder="原始金額">
                    <input type="text" class="math-input" name="remaining_amount" required placeholder="剩餘金額">
                    <input type="text" class="math-input" name="monthly_payment" value="0" required placeholder="月還款">
                    <button type="submit">新增</button>
                </form>
                {debt_rows}
            </div>

            <div class="card" id="transactions">
                <h2>最近 100 筆記帳</h2>
                <div class="table-wrap">
                    <table>
                        <thead>
                            <tr><th>時間</th><th>帳戶</th><th>類型</th>
                            <th>分類</th><th>項目</th><th>金額</th><th>操作</th></tr>
                        </thead>
                        <tbody>{rows}</tbody>
                    </table>
                </div>
            </div>
        </div>
        <script>
            document.querySelectorAll(".math-input").forEach((input) => {{
                const box = document.createElement("div");
                box.className = "quick-math";
                ["-1000", "-500", "-100", "+100", "+500", "+1000"].forEach((value) => {{
                    const button = document.createElement("button");
                    button.type = "button";
                    button.textContent = value;
                    button.addEventListener("click", () => {{
                        input.value = value;
                        input.focus();
                    }});
                    box.appendChild(button);
                }});
                const hint = document.createElement("small");
                hint.className = "math-hint";
                hint.textContent = "可輸入 5000、+500、-100、*2、/2、+500-100";
                input.insertAdjacentElement("afterend", box);
                box.insertAdjacentElement("afterend", hint);
            }});
        </script>
    </body>
    </html>
    """


@app.route("/admin", methods=["GET"])
def admin_home():
    return admin_page(request.args.get("message", ""))


@app.route("/admin/login", methods=["POST"])
def admin_login():
    if not ADMIN_PASSWORD:
        return admin_page("尚未設定 ADMIN_PASSWORD 環境變數。"), 503

    if request.form.get("password", "") != ADMIN_PASSWORD:
        return admin_page("管理密碼錯誤。"), 401

    session["finance_admin"] = True
    return redirect(url_for("admin_home"))


@app.route("/admin/logout", methods=["GET"])
def admin_logout():
    session.clear()
    return redirect(url_for("admin_home"))


@app.route("/admin/transaction/add", methods=["POST"])
def admin_add_transaction():
    if not admin_logged_in():
        return redirect(url_for("admin_home"))

    description = request.form.get("description", "").strip()
    transaction_type = request.form.get("type", "").strip()
    account = normalize_account(request.form.get("account"))
    amount = parse_positive_int(request.form.get("amount", ""))
    category = request.form.get("category", "").strip()

    if amount is None or transaction_type not in {"收入", "支出"}:
        return redirect(url_for("admin_home", message="新增資料格式錯誤。"))

    if not category:
        category = (
            classify_income(description)
            if transaction_type == "收入"
            else classify_expense(description)
        )

    try:
        (
            supabase
            .table("transactions")
            .insert(
                {
                    "line_user_id": "WEB_ADMIN",
                    "account": account,
                    "type": transaction_type,
                    "category": category,
                    "amount": amount,
                    "description": description,
                }
            )
            .execute()
        )
    except Exception as error:
        return redirect(url_for("admin_home", message=f"新增失敗：{error}"))

    return redirect(url_for("admin_home", message="新增成功。"))


@app.route("/admin/transaction/<transaction_id>/edit", methods=["GET", "POST"])
def admin_edit_transaction(transaction_id: str):
    if not admin_logged_in():
        return redirect(url_for("admin_home"))

    if request.method == "POST":
        amount = parse_positive_int(request.form.get("amount", ""))
        if amount is None:
            return redirect(url_for("admin_home", message="金額格式錯誤。"))

        try:
            update_transaction_record(
                transaction_id,
                transaction_type=request.form.get("type"),
                category=request.form.get("category"),
                amount=amount,
                description=request.form.get("description"),
                account=request.form.get("account"),
            )
        except Exception as error:
            return redirect(url_for("admin_home", message=f"修改失敗：{error}"))

        return redirect(url_for("admin_home", message="修改成功。"))

    response = (
        supabase
        .table("transactions")
        .select("*")
        .eq("id", transaction_id)
        .limit(1)
        .execute()
    )
    rows = response.data or []
    if not rows:
        return redirect(url_for("admin_home", message="找不到這筆記帳。"))

    item = rows[0]
    account = normalize_account(item.get("account"))
    transaction_type = str(item.get("type") or "支出")

    return f"""
    <!doctype html><html lang="zh-Hant"><head>
    <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>編輯記帳</title>
    <style>
        body {{font-family:-apple-system,"Microsoft JhengHei";background:#f5f5f7;padding:24px}}
        form {{max-width:520px;margin:auto;background:white;padding:26px;border-radius:22px;
        box-shadow:0 16px 45px rgba(0,0,0,.09)}}
        input,select,button,a {{display:block;width:100%;padding:12px;margin-top:12px;
        border-radius:11px;border:1px solid #ddd;box-sizing:border-box;font-size:16px}}
        button {{background:#007aff;color:white;border:0;font-weight:700}}
        a {{text-align:center;text-decoration:none;color:#007aff}}
    </style></head><body>
    <form method="post">
        <h1>編輯記帳</h1>
        <select name="account">
            <option value="個人" {'selected' if account == '個人' else ''}>個人</option>
            <option value="金家水電" {'selected' if account == '金家水電' else ''}>金家水電</option>
        </select>
        <select name="type">
            <option value="支出" {'selected' if transaction_type == '支出' else ''}>支出</option>
            <option value="收入" {'selected' if transaction_type == '收入' else ''}>收入</option>
        </select>
        <input name="description" required value="{escape(str(item.get('description') or ''))}">
        <input name="category" required value="{escape(str(item.get('category') or ''))}">
        <input name="amount" type="number" min="1" required value="{int(item.get('amount') or 0)}">
        <button type="submit">儲存修改</button>
        <a href="/admin">取消</a>
    </form></body></html>
    """


@app.route("/admin/transaction/<transaction_id>/delete", methods=["POST"])
def admin_delete_transaction(transaction_id: str):
    if not admin_logged_in():
        return redirect(url_for("admin_home"))

    try:
        delete_transaction_record(transaction_id)
    except Exception as error:
        return redirect(url_for("admin_home", message=f"刪除失敗：{error}"))

    return redirect(url_for("admin_home", message="刪除成功。"))


@app.route("/admin/bank/update", methods=["POST"])
def admin_update_bank():
    if not admin_logged_in():
        return redirect(url_for("admin_home"))

    owner = request.form.get("owner", "")
    bank_name = normalize_bank_name(request.form.get("bank_name", ""))
    balance_text = request.form.get("balance", "").strip()

    if owner not in {"個人", "金家"} or not bank_name:
        return redirect(url_for("admin_home", message="銀行資料格式錯誤。"))

    try:
        current_balance = int(
            get_bank_balances(owner)
            .get(bank_name, {})
            .get("balance", 0)
            or 0
        )
        new_balance = safe_eval_number_expression(
            balance_text,
            current_balance,
        )
        set_bank_balance(owner, bank_name, new_balance)
        write_audit_log(
            action="update",
            source="WEB",
            entity_type="bank_balance",
            entity_id=f"{owner}:{bank_name}",
            before_data={"balance": current_balance},
            after_data={"balance": new_balance, "expression": balance_text},
        )
    except Exception as error:
        return redirect(url_for("admin_home", message=f"銀行更新失敗：{error}"))

    return redirect(url_for("admin_home", message=f"{bank_name}已更新。"))


@app.route("/admin/credit-card/update", methods=["POST"])
def admin_update_credit_card():
    if not admin_logged_in():
        return redirect(url_for("admin_home"))

    card_name = request.form.get("card_name", "")
    try:
        current_card = get_credit_cards().get(card_name, {})
        total_limit = safe_eval_number_expression(
            request.form.get("total_limit", "0"),
            int(current_card.get("total_limit") or 0),
        )
        available_limit = safe_eval_number_expression(
            request.form.get("available_limit", "0"),
            int(current_card.get("available_limit") or 0),
        )
        statement_amount = safe_eval_number_expression(
            request.form.get("statement_amount", "0"),
            int(current_card.get("statement_amount") or 0),
        )
        statement_day_text = request.form.get("statement_day", "").strip()
        due_day_text = request.form.get("due_day", "").strip()
        statement_day = int(statement_day_text) if statement_day_text else 0
        due_day = int(due_day_text) if due_day_text else 0
        payment_status = request.form.get("payment_status", "未繳交")

        set_credit_card_values(
            card_name,
            total_limit=total_limit,
            available_limit=available_limit,
            statement_day=statement_day,
            due_day=due_day,
            statement_amount=statement_amount,
            payment_status=payment_status,
        )
        write_audit_log(
            action="update",
            source="WEB",
            entity_type="credit_card",
            entity_id=card_name,
            before_data=current_card,
            after_data={
                "total_limit": total_limit,
                "available_limit": available_limit,
                "statement_day": statement_day,
                "due_day": due_day,
                "statement_amount": statement_amount,
                "payment_status": payment_status,
            },
        )
    except Exception as error:
        return redirect(url_for("admin_home", message=f"信用卡更新失敗：{error}"))

    return redirect(url_for("admin_home", message=f"{card_name}已更新。"))


@app.route("/admin/jinjia-status/update", methods=["POST"])
def admin_update_jinjia_status():
    if not admin_logged_in():
        return redirect(url_for("admin_home"))

    try:
        month = request.form.get("month", "")
        item_type = request.form.get("item_type", "")
        item_name = request.form.get("item_name", "")
        status = request.form.get("status", "未繳交")
        current_rows = get_jinjia_statuses(month)
        current_row = next(
            (
                row for row in current_rows
                if row.get("item_type") == item_type
                and row.get("item_name") == item_name
            ),
            {},
        )
        amount = safe_eval_number_expression(
            request.form.get("amount", "0"),
            int(current_row.get("amount") or 0),
        )

        if item_type not in {"帳單", "人物"}:
            raise ValueError("類型錯誤")
        if status not in {"已繳交", "未繳交"}:
            raise ValueError("狀態錯誤")
        if amount < 0:
            raise ValueError("金額不可小於 0")

        update_jinjia_status(
            month,
            item_type,
            item_name,
            status,
            amount,
        )
        write_audit_log(
            action="update",
            source="WEB",
            entity_type="jinjia_status",
            entity_id=f"{month}:{item_type}:{item_name}",
            before_data=current_row,
            after_data={"status": status, "amount": amount},
        )
    except Exception as error:
        return redirect(url_for("admin_home", message=f"金家狀態更新失敗：{error}"))

    return redirect(url_for("admin_home", message=f"{item_name}已更新。"))


@app.route("/admin/debt/add", methods=["POST"])
def admin_add_debt():
    if not admin_logged_in():
        return redirect(url_for("admin_home"))

    try:
        original_amount = safe_eval_number_expression(
            request.form.get("original_amount", "0"), 0
        )
        remaining_amount = safe_eval_number_expression(
            request.form.get("remaining_amount", "0"), 0
        )
        monthly_payment = safe_eval_number_expression(
            request.form.get("monthly_payment", "0"), 0
        )
        if min(original_amount, remaining_amount, monthly_payment) < 0:
            raise ValueError("金額不可小於 0")

        (
            supabase
            .table("debts")
            .insert(
                {
                    "line_user_id": "WEB_ADMIN",
                    "debt_name": request.form.get("debt_name", "").strip(),
                    "debt_type": request.form.get("debt_type", "其他").strip(),
                    "original_amount": original_amount,
                    "remaining_amount": remaining_amount,
                    "monthly_payment": monthly_payment,
                }
            )
            .execute()
        )
    except Exception as error:
        return redirect(url_for("admin_home", message=f"新增負債失敗：{error}"))

    return redirect(url_for("admin_home", message="負債新增成功。"))


@app.route("/admin/debt/<debt_id>/update", methods=["POST"])
def admin_update_debt(debt_id: str):
    if not admin_logged_in():
        return redirect(url_for("admin_home"))

    try:
        debt_response = (
            supabase
            .table("debts")
            .select("*")
            .eq("id", debt_id)
            .limit(1)
            .execute()
        )
        current_debt_rows = debt_response.data or []
        if not current_debt_rows:
            raise ValueError("找不到負債資料")
        current_debt = current_debt_rows[0]

        original_amount = safe_eval_number_expression(
            request.form.get("original_amount", "0"),
            int(current_debt.get("original_amount") or 0),
        )
        remaining_amount = safe_eval_number_expression(
            request.form.get("remaining_amount", "0"),
            int(current_debt.get("remaining_amount") or 0),
        )
        monthly_payment = safe_eval_number_expression(
            request.form.get("monthly_payment", "0"),
            int(current_debt.get("monthly_payment") or 0),
        )
        if min(original_amount, remaining_amount, monthly_payment) < 0:
            raise ValueError("金額不可小於 0")

        (
            supabase
            .table("debts")
            .update(
                {
                    "debt_name": request.form.get("debt_name", "").strip(),
                    "debt_type": request.form.get("debt_type", "其他").strip(),
                    "original_amount": original_amount,
                    "remaining_amount": remaining_amount,
                    "monthly_payment": monthly_payment,
                }
            )
            .eq("id", debt_id)
            .execute()
        )
    except Exception as error:
        return redirect(url_for("admin_home", message=f"負債更新失敗：{error}"))

    return redirect(url_for("admin_home", message="負債已更新。"))


@app.route("/admin/debt/<debt_id>/delete", methods=["POST"])
def admin_delete_debt(debt_id: str):
    if not admin_logged_in():
        return redirect(url_for("admin_home"))

    try:
        supabase.table("debts").delete().eq("id", debt_id).execute()
    except Exception as error:
        return redirect(url_for("admin_home", message=f"負債刪除失敗：{error}"))

    return redirect(url_for("admin_home", message="負債已刪除。"))



# ----------------------------
# 2.0 Smart Finance
# ----------------------------

def get_smart_rows(table_name: str, *, order_by: str = "created_at") -> list[dict[str, Any]]:
    try:
        response = (
            supabase.table(table_name)
            .select("*")
            .order(order_by, desc=True)
            .execute()
        )
        return response.data or []
    except Exception as error:
        print(f"讀取 {table_name} 失敗：", error)
        return []


def calculate_smart_summary() -> dict[str, Any]:
    now = datetime.now(TAIPEI)
    month = now.strftime("%Y-%m")
    try:
        response = (
            supabase.table("transactions")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        )
        transactions = response.data or []
    except Exception as error:
        print("讀取智慧分析交易失敗：", error)
        transactions = []

    monthly = [
        row for row in transactions
        if str(row.get("created_at") or "").startswith(month)
        and normalize_account(row.get("account")) == "個人"
    ]
    income = sum(float(row.get("amount") or 0) for row in monthly if row.get("type") == "收入")
    expense = sum(float(row.get("amount") or 0) for row in monthly if row.get("type") == "支出")
    by_category: dict[str, float] = {}
    daily: dict[str, float] = {}
    for row in monthly:
        if row.get("type") != "支出":
            continue
        amount = float(row.get("amount") or 0)
        category = str(row.get("category") or "其他")
        by_category[category] = by_category.get(category, 0) + amount
        day = str(row.get("created_at") or "")[:10]
        daily[day] = daily.get(day, 0) + amount

    elapsed_days = max(now.day, 1)
    days_in_month = 31
    for candidate in (28, 29, 30, 31):
        try:
            datetime(now.year, now.month, candidate)
            days_in_month = candidate
        except ValueError:
            break
    projected_expense = expense / elapsed_days * days_in_month if expense else 0
    top_category = max(by_category, key=by_category.get) if by_category else "尚無資料"
    saving_rate = ((income - expense) / income * 100) if income > 0 else 0

    budgets = get_smart_rows("budgets", order_by="updated_at")
    budget_rows = []
    for item in budgets:
        if str(item.get("month") or "") not in {month, "每月"}:
            continue
        category = str(item.get("category") or "其他")
        limit_amount = float(item.get("amount") or 0)
        spent = by_category.get(category, 0)
        percent = spent / limit_amount * 100 if limit_amount else 0
        budget_rows.append({**item, "spent": spent, "remaining": limit_amount-spent, "percent": percent})

    goals = get_smart_rows("saving_goals", order_by="updated_at")
    for goal in goals:
        target = float(goal.get("target_amount") or 0)
        current = float(goal.get("current_amount") or 0)
        goal["percent"] = current / target * 100 if target else 0

    recurring = get_smart_rows("recurring_items", order_by="updated_at")
    notifications = get_smart_rows("finance_notifications", order_by="created_at")[:20]

    advice = []
    if income <= 0:
        advice.append("本月尚未記錄收入，建議先補上薪資或其他收入。")
    elif saving_rate < 10:
        advice.append("本月儲蓄率低於 10%，可先從最大支出分類減少 5% 開始。")
    elif saving_rate >= 30:
        advice.append("本月儲蓄率表現良好，可以把部分結餘分配到儲蓄目標。")
    if projected_expense > income and income > 0:
        advice.append("依目前速度，月底支出可能超過收入，建議立即檢查非必要消費。")
    if top_category != "尚無資料":
        advice.append(f"本月最大支出分類是「{top_category}」，可優先檢查這一類。")
    over_budget = [row for row in budget_rows if row["percent"] > 100]
    if over_budget:
        advice.append("已有預算超支項目：" + "、".join(str(row.get("category")) for row in over_budget))
    if not advice:
        advice.append("本月資料正常，持續記帳即可累積更準確的分析。")

    return {
        "month": month,
        "income": income,
        "expense": expense,
        "balance": income-expense,
        "saving_rate": saving_rate,
        "projected_expense": projected_expense,
        "top_category": top_category,
        "categories": sorted(by_category.items(), key=lambda x: x[1], reverse=True),
        "daily": sorted(daily.items()),
        "budgets": budget_rows,
        "goals": goals,
        "recurring": recurring,
        "notifications": notifications,
        "advice": advice,
    }


def smart_page_html(summary: dict[str, Any], *, admin: bool = False, message: str = "") -> str:
    notice = f'<div class="notice">{escape(message)}</div>' if message else ""
    category_rows = "".join(
        f'<tr><td>{escape(name)}</td><td>NT$ {int(amount):,}</td></tr>'
        for name, amount in summary["categories"]
    ) or '<tr><td colspan="2">尚無支出資料</td></tr>'

    budget_cards = ""
    for row in summary["budgets"]:
        percent = min(max(float(row["percent"]), 0), 100)
        budget_cards += f'''<div class="item-card"><h3>{escape(str(row.get("category") or ""))}</h3>
        <p>預算 NT$ {int(row.get("amount") or 0):,}｜已用 NT$ {int(row["spent"]):,}</p>
        <div class="bar"><span style="width:{percent:.1f}%"></span></div>
        <small>剩餘 NT$ {int(row["remaining"]):,}｜{row["percent"]:.1f}%</small></div>'''
    if not budget_cards:
        budget_cards = '<div class="empty">尚未設定預算。</div>'

    goal_cards = ""
    for row in summary["goals"]:
        percent = min(max(float(row["percent"]), 0), 100)
        goal_cards += f'''<div class="item-card"><h3>{escape(str(row.get("name") or ""))}</h3>
        <p>NT$ {int(row.get("current_amount") or 0):,} / NT$ {int(row.get("target_amount") or 0):,}</p>
        <div class="bar goal"><span style="width:{percent:.1f}%"></span></div>
        <small>{row["percent"]:.1f}%｜期限 {escape(str(row.get("target_date") or "未設定"))}</small></div>'''
    if not goal_cards:
        goal_cards = '<div class="empty">尚未建立儲蓄目標。</div>'

    recurring_rows = "".join(
        f'<tr><td>{escape(str(row.get("name") or ""))}</td><td>{escape(str(row.get("item_type") or ""))}</td><td>NT$ {int(row.get("amount") or 0):,}</td><td>每月 {int(row.get("day_of_month") or 1)} 日</td><td>{"啟用" if row.get("is_active", True) else "停用"}</td></tr>'
        for row in summary["recurring"]
    ) or '<tr><td colspan="5">尚無固定收支。</td></tr>'

    advice_html = "".join(f'<li>{escape(text)}</li>' for text in summary["advice"])
    admin_link = '<a class="button" href="/admin/smart">管理 2.0 功能</a>' if not admin else '<a class="button secondary" href="/smart">查看智慧首頁</a><a class="button secondary" href="/admin">返回管理後台</a>'

    admin_forms = ""
    if admin:
        admin_forms = f'''
        <section><h2>新增／更新預算</h2><form method="post" action="/admin/smart/budget" class="form-grid">
        <input name="category" placeholder="分類，例如：飲食" required><input name="amount" placeholder="預算金額" required>
        <input name="month" value="{escape(summary['month'])}" required><button>儲存預算</button></form></section>
        <section><h2>新增儲蓄目標</h2><form method="post" action="/admin/smart/goal" class="form-grid">
        <input name="name" placeholder="目標，例如：日本旅遊" required><input name="target_amount" placeholder="目標金額" required>
        <input name="current_amount" placeholder="目前金額" value="0" required><input type="date" name="target_date"><button>新增目標</button></form></section>
        <section><h2>新增固定收入／支出</h2><form method="post" action="/admin/smart/recurring" class="form-grid">
        <input name="name" placeholder="名稱，例如：Netflix" required><select name="item_type"><option>支出</option><option>收入</option></select>
        <input name="amount" placeholder="金額" required><input type="number" min="1" max="31" name="day_of_month" value="1" required>
        <input name="category" placeholder="分類"><button>新增固定項目</button></form></section>
        <section><h2>目標快速存款</h2><form method="post" action="/admin/smart/goal/deposit" class="form-grid">
        <input name="name" placeholder="目標名稱" required><input name="amount" placeholder="輸入 +500 或 -100" required><button>更新進度</button></form></section>
        '''

    return f'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>AI 財務管家 2.0</title><style>
    *{{box-sizing:border-box}}body{{margin:0;background:#f5f5f7;color:#1d1d1f;font-family:-apple-system,"Microsoft JhengHei",sans-serif}}
    .wrap{{max-width:1180px;margin:auto;padding:24px}}header{{display:flex;justify-content:space-between;gap:16px;align-items:center;flex-wrap:wrap}}
    .button,button{{display:inline-block;background:#007aff;color:white;border:0;border-radius:12px;padding:11px 16px;text-decoration:none;font-weight:700;margin:3px}}
    .secondary{{background:#e8e8ed;color:#1d1d1f}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px;margin:20px 0}}
    .card,section{{background:white;border-radius:22px;padding:20px;box-shadow:0 8px 30px rgba(0,0,0,.05);margin-bottom:16px}}
    .card .value{{font-size:28px;font-weight:800}}h1,h2,h3{{margin-top:0}}.two{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
    .item-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}}.item-card{{border:1px solid #e5e5ea;padding:15px;border-radius:16px}}
    .bar{{height:10px;background:#eee;border-radius:10px;overflow:hidden}}.bar span{{display:block;height:100%;background:#ff9500}}.bar.goal span{{background:#34c759}}
    table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;border-bottom:1px solid #eee;text-align:left}}ul{{line-height:1.8}}
    .form-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}}input,select{{padding:12px;border:1px solid #d2d2d7;border-radius:11px;font-size:15px}}
    .notice{{padding:12px;background:#fff3cd;border-radius:12px;margin:12px 0}}.empty{{color:#6e6e73}}@media(max-width:760px){{.two{{grid-template-columns:1fr}}}}
    </style></head><body><div class="wrap"><header><div><h1>AI 財務管家 2.0</h1><p>{escape(summary['month'])} 智慧分析與目標管理</p></div><div>{admin_link}</div></header>{notice}
    <div class="grid"><div class="card"><small>本月收入</small><div class="value">NT$ {int(summary['income']):,}</div></div>
    <div class="card"><small>本月支出</small><div class="value">NT$ {int(summary['expense']):,}</div></div>
    <div class="card"><small>本月結餘</small><div class="value">NT$ {int(summary['balance']):,}</div></div>
    <div class="card"><small>儲蓄率</small><div class="value">{summary['saving_rate']:.1f}%</div></div>
    <div class="card"><small>月底預估支出</small><div class="value">NT$ {int(summary['projected_expense']):,}</div></div>
    <div class="card"><small>最大支出分類</small><div class="value">{escape(summary['top_category'])}</div></div></div>
    <section><h2>AI 財務建議</h2><ul>{advice_html}</ul></section>
    <div class="two"><section><h2>分類支出</h2><table><thead><tr><th>分類</th><th>金額</th></tr></thead><tbody>{category_rows}</tbody></table></section>
    <section><h2>固定收入／支出</h2><table><thead><tr><th>名稱</th><th>類型</th><th>金額</th><th>日期</th><th>狀態</th></tr></thead><tbody>{recurring_rows}</tbody></table></section></div>
    <section><h2>預算進度</h2><div class="item-grid">{budget_cards}</div></section>
    <section><h2>儲蓄目標</h2><div class="item-grid">{goal_cards}</div></section>{admin_forms}
    <footer><small>AI Finance Manager · Version {APP_VERSION}</small></footer></div></body></html>'''


@app.route("/smart", methods=["GET"])
def smart_dashboard():
    return smart_page_html(calculate_smart_summary())


@app.route("/admin/smart", methods=["GET"])
def admin_smart():
    if not admin_logged_in():
        return redirect(url_for("admin_home"))
    return smart_page_html(
        calculate_smart_summary(),
        admin=True,
        message=request.args.get("message", ""),
    )


@app.route("/admin/smart/budget", methods=["POST"])
def admin_smart_budget():
    if not admin_logged_in():
        return redirect(url_for("admin_home"))
    try:
        category = request.form.get("category", "").strip()
        month = request.form.get("month", "").strip() or datetime.now(TAIPEI).strftime("%Y-%m")
        amount = safe_eval_number_expression(request.form.get("amount", "0"), 0)
        if not category:
            raise ValueError("分類不可空白")
        existing = supabase.table("budgets").select("id").eq("category", category).eq("month", month).limit(1).execute().data or []
        payload = {"category": category, "month": month, "amount": amount, "updated_at": datetime.now(TAIPEI).isoformat()}
        if existing:
            supabase.table("budgets").update(payload).eq("id", existing[0]["id"]).execute()
        else:
            supabase.table("budgets").insert(payload).execute()
        write_audit_log(action="upsert", source="WEB", entity_type="budget", entity_id=f"{month}:{category}", after_data=payload)
        message = "預算已儲存。"
    except Exception as error:
        message = f"預算儲存失敗：{error}"
    return redirect(url_for("admin_smart", message=message))


@app.route("/admin/smart/goal", methods=["POST"])
def admin_smart_goal():
    if not admin_logged_in():
        return redirect(url_for("admin_home"))
    try:
        payload = {
            "name": request.form.get("name", "").strip(),
            "target_amount": safe_eval_number_expression(request.form.get("target_amount", "0"), 0),
            "current_amount": safe_eval_number_expression(request.form.get("current_amount", "0"), 0),
            "target_date": request.form.get("target_date") or None,
            "updated_at": datetime.now(TAIPEI).isoformat(),
        }
        if not payload["name"]:
            raise ValueError("目標名稱不可空白")
        supabase.table("saving_goals").insert(payload).execute()
        write_audit_log(action="create", source="WEB", entity_type="saving_goal", entity_id=payload["name"], after_data=payload)
        message = "儲蓄目標已新增。"
    except Exception as error:
        message = f"新增目標失敗：{error}"
    return redirect(url_for("admin_smart", message=message))


@app.route("/admin/smart/goal/deposit", methods=["POST"])
def admin_smart_goal_deposit():
    if not admin_logged_in():
        return redirect(url_for("admin_home"))
    try:
        name = request.form.get("name", "").strip()
        rows = supabase.table("saving_goals").select("*").eq("name", name).limit(1).execute().data or []
        if not rows:
            raise ValueError("找不到這個儲蓄目標")
        row = rows[0]
        current = int(row.get("current_amount") or 0)
        new_value = safe_eval_number_expression(request.form.get("amount", "0"), current)
        supabase.table("saving_goals").update({"current_amount": new_value, "updated_at": datetime.now(TAIPEI).isoformat()}).eq("id", row["id"]).execute()
        write_audit_log(action="update", source="WEB", entity_type="saving_goal", entity_id=row["id"], before_data={"current_amount": current}, after_data={"current_amount": new_value})
        message = f"{name} 已更新為 NT$ {new_value:,}。"
    except Exception as error:
        message = f"更新目標失敗：{error}"
    return redirect(url_for("admin_smart", message=message))


@app.route("/admin/smart/recurring", methods=["POST"])
def admin_smart_recurring():
    if not admin_logged_in():
        return redirect(url_for("admin_home"))
    try:
        payload = {
            "name": request.form.get("name", "").strip(),
            "item_type": request.form.get("item_type", "支出"),
            "amount": safe_eval_number_expression(request.form.get("amount", "0"), 0),
            "day_of_month": int(request.form.get("day_of_month", "1")),
            "category": request.form.get("category", "其他").strip() or "其他",
            "is_active": True,
            "updated_at": datetime.now(TAIPEI).isoformat(),
        }
        if not payload["name"] or payload["item_type"] not in {"收入", "支出"} or not 1 <= payload["day_of_month"] <= 31:
            raise ValueError("固定收支資料格式錯誤")
        supabase.table("recurring_items").insert(payload).execute()
        write_audit_log(action="create", source="WEB", entity_type="recurring_item", entity_id=payload["name"], after_data=payload)
        message = "固定收入／支出已新增。"
    except Exception as error:
        message = f"新增固定項目失敗：{error}"
    return redirect(url_for("admin_smart", message=message))


@app.route("/api/smart/summary", methods=["GET"])
def smart_summary_api():
    return jsonify(calculate_smart_summary())


# ----------------------------
# AI 財務管家 4.0 Ultimate Alpha
# ----------------------------

def calculate_jarvis_summary() -> dict[str, Any]:
    smart = calculate_smart_summary()
    try:
        banks = get_bank_balances("個人")
        cash = sum(int(v.get("balance") or 0) for v in banks.values())
    except Exception as error:
        print("4.0 bank error:", error)
        banks, cash = {}, 0
    try:
        cards = get_credit_cards()
        total_limit = sum(int(v.get("total_limit") or 0) for v in cards.values())
        available = sum(int(v.get("available_limit") or 0) for v in cards.values())
        credit_used = max(total_limit - available, 0)
        credit_ratio = credit_used / total_limit * 100 if total_limit else 0
    except Exception as error:
        print("4.0 card error:", error)
        cards, total_limit, available, credit_used, credit_ratio = {}, 0, 0, 0, 0
    try:
        rows = supabase.table("debts").select("remaining_amount").execute().data or []
        debt = sum(float(v.get("remaining_amount") or 0) for v in rows)
    except Exception as error:
        print("4.0 debt error:", error)
        debt = 0

    budget_total = sum(float(v.get("amount") or 0) for v in smart.get("budgets", []))
    budget_used = sum(float(v.get("spent") or 0) for v in smart.get("budgets", []))
    budget_ratio = budget_used / budget_total * 100 if budget_total else 0
    target = sum(float(v.get("target_amount") or 0) for v in smart.get("goals", []))
    current = sum(float(v.get("current_amount") or 0) for v in smart.get("goals", []))
    goal_ratio = current / target * 100 if target else 0

    score = 100
    if smart.get("balance", 0) < 0: score -= 30
    if credit_ratio > 50: score -= 20
    elif credit_ratio > 30: score -= 10
    if smart.get("saving_rate", 0) < 10: score -= 20
    elif smart.get("saving_rate", 0) < 20: score -= 10
    if debt > cash and debt > 0: score -= 15
    score = max(0, min(100, score))

    risk = "低" if score >= 80 else "中" if score >= 60 else "高"
    return {
        **smart, "banks": banks, "cards": cards, "cash": cash, "debt": debt,
        "net_worth": cash - debt, "credit_used": credit_used,
        "credit_available": available, "credit_total": total_limit,
        "credit_ratio": credit_ratio, "budget_ratio": budget_ratio,
        "goal_ratio": goal_ratio, "health_score": score, "risk": risk,
    }


def _v4_money(value: float | int) -> str:
    return f"{int(value):,}"


def _v4_progress(value: float) -> float:
    return min(max(float(value), 0), 100)


def _v4_sparkline(values: list[float]) -> str:
    if not values:
        values = [0, 0, 0, 0, 0, 0]
    maximum = max(max(values), 1)
    width, height = 520, 150
    step = width / max(len(values) - 1, 1)
    points = []
    for index, value in enumerate(values):
        x = index * step
        y = height - (float(value) / maximum * (height - 24)) - 12
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def jarvis_layout(body: str, active: str, title: str, boot: bool = False) -> str:
    links = [
        ("garage", "/jarvis", "⌂", "財務總覽"),
        ("command", "/jarvis/command", "✦", "AI 助理"),
        ("private", "/jarvis/private", "▣", "個人報告"),
        ("themes", "/jarvis/themes", "◈", "主題設定"),
        ("admin", "/admin", "⚙", "管理中心"),
    ]
    nav = "".join(f'<a class="nav-item {"active" if active == k else ""}" href="{u}"><span class="nav-icon">{i}</span><span>{n}</span></a>' for k,u,i,n in links)
    mobile_nav = "".join(f'<a class="mobile-nav-item {"active" if active == k else ""}" href="{u}"><span>{i}</span><small>{n.replace("財務總覽","總覽").replace("個人報告","報告").replace("主題設定","主題").replace("管理中心","管理")}</small></a>' for k,u,i,n in links)
    boot_html = '<div id="boot" class="boot"><div class="boot-orbit"><div class="boot-core">財</div></div><h1>AI 財務管家</h1><p id="boot-line">正在啟動超跑財務中控...</p><div class="boot-track"><i></i></div></div>' if boot else ""
    css = r'''
    :root{--bg:#03060d;--bg2:#07101c;--panel:rgba(7,14,26,.84);--line:rgba(93,215,255,.18);--text:#f4fbff;--muted:#8ea4b7;--accent:#36d6ff;--purple:#7b61ff;--green:#28e5a6;--red:#ff5b78;--gold:#ffc857;--orange:#ff9f43;--title:DFKai-SB,"標楷體",KaiTi,serif;--ui:"Noto Sans TC","Microsoft JhengHei",system-ui,sans-serif}
    *{box-sizing:border-box}html{background:var(--bg)}body{margin:0;min-height:100vh;color:var(--text);font-family:var(--ui);background:radial-gradient(circle at 83% 0%,rgba(92,65,255,.19),transparent 35%),radial-gradient(circle at 8% 30%,rgba(0,210,255,.11),transparent 28%),linear-gradient(145deg,var(--bg),var(--bg2) 58%,#030710);overflow-x:hidden}body:before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.16;background-image:linear-gradient(45deg,rgba(255,255,255,.018) 25%,transparent 25%,transparent 75%,rgba(255,255,255,.018) 75%),linear-gradient(45deg,rgba(255,255,255,.018) 25%,transparent 25%,transparent 75%,rgba(255,255,255,.018) 75%);background-size:8px 8px;background-position:0 0,4px 4px}a{color:inherit;text-decoration:none}
    .app-shell{min-height:100vh;display:grid;grid-template-columns:250px minmax(0,1fr)}.sidebar{position:sticky;top:0;height:100vh;padding:22px 16px;border-right:1px solid var(--line);background:rgba(4,10,19,.93);backdrop-filter:blur(22px);z-index:30;display:flex;flex-direction:column}.brand-block{display:flex;align-items:center;gap:12px;padding:7px 8px 24px}.brand-mark{width:48px;height:48px;border-radius:16px;display:grid;place-items:center;font:25px var(--title);background:conic-gradient(from 200deg,var(--accent),var(--purple),var(--green),var(--accent));box-shadow:0 0 30px rgba(54,214,255,.25);position:relative}.brand-mark:after{content:"";position:absolute;inset:3px;border-radius:13px;background:#07101c}.brand-mark span{z-index:1}.brand-text b,.mobile-brand b{font-family:var(--title);font-size:18px}.brand-text small,.mobile-brand small{display:block;color:var(--muted);font-size:10px;margin-top:3px}.nav-stack{display:grid;gap:8px}.nav-item{position:relative;display:flex;align-items:center;gap:13px;padding:12px 14px;border-radius:14px;color:#9fb2c3;transition:.25s;overflow:hidden}.nav-item:before{content:"";position:absolute;inset:0;background:linear-gradient(90deg,rgba(54,214,255,.14),transparent);opacity:0}.nav-item:hover,.nav-item.active{color:#fff;transform:translateX(3px);border:1px solid rgba(75,211,255,.18)}.nav-item:hover:before,.nav-item.active:before{opacity:1}.nav-item.active:after{content:"";position:absolute;left:0;top:20%;bottom:20%;width:3px;border-radius:5px;background:var(--accent);box-shadow:0 0 16px var(--accent)}.nav-icon{width:26px;text-align:center;font-size:19px;z-index:1}.nav-item span:last-child{z-index:1;font-size:14px}.side-status{margin-top:auto;border:1px solid var(--line);background:rgba(10,20,35,.66);border-radius:17px;padding:14px}.online{display:flex;align-items:center;gap:8px;color:#bfffe8;font-size:12px}.pulse-dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 13px var(--green);animation:pulse 1.8s ease-in-out infinite}.side-status small{display:block;color:var(--muted);margin-top:7px;line-height:1.5}
    .content-wrap{min-width:0}.topbar{height:78px;position:sticky;top:0;z-index:20;display:flex;align-items:center;justify-content:space-between;padding:0 28px;border-bottom:1px solid rgba(98,205,255,.12);background:rgba(3,8,16,.74);backdrop-filter:blur(18px)}.top-title h1{font:21px var(--title);margin:0}.top-title p{margin:3px 0 0;color:var(--muted);font-size:11px}.top-actions{display:flex;gap:10px}.status-pill,.icon-btn{height:40px;border:1px solid var(--line);background:rgba(10,21,36,.8);border-radius:13px;display:flex;align-items:center;justify-content:center}.status-pill{padding:0 14px;gap:8px;font-size:12px}.icon-btn{width:40px}.mobile-header,.mobile-nav{display:none}main{padding:24px 28px 110px;max-width:1700px;margin:auto}.dashboard-grid{display:grid;gap:18px}.hero-grid{grid-template-columns:minmax(0,1.55fr) minmax(310px,.75fr)}
    .panel{position:relative;border:1px solid var(--line);background:linear-gradient(145deg,var(--panel),rgba(12,23,40,.8));box-shadow:0 18px 50px rgba(0,0,0,.42),inset 0 1px rgba(255,255,255,.025);border-radius:22px;overflow:hidden;isolation:isolate}.panel:after{content:"";position:absolute;top:-40%;left:-80%;width:45%;height:180%;background:linear-gradient(90deg,transparent,rgba(255,255,255,.075),transparent);transform:skewX(-20deg);animation:scan 9s ease-in-out infinite;pointer-events:none}.reveal{opacity:0;transform:translateY(16px) scale(.99);animation:reveal .6s cubic-bezier(.2,.8,.2,1) forwards;animation-delay:var(--delay,0s)}
    .hero-card{min-height:300px;padding:30px;display:grid;grid-template-columns:1fr 250px;align-items:center;background:radial-gradient(circle at 80% 50%,rgba(54,214,255,.15),transparent 33%),linear-gradient(145deg,rgba(8,18,32,.97),rgba(7,12,24,.9))}.eyebrow{font-size:11px;letter-spacing:2px;color:#81dfff}.hero-card h2,.section-title h3,.ai-card h3{font-family:var(--title)}.hero-card h2{font-size:30px;margin:10px 0 7px}.hero-money{font-size:clamp(42px,5.2vw,78px);font-weight:850;letter-spacing:-3px;line-height:1.03;white-space:nowrap;text-shadow:0 0 30px rgba(54,214,255,.18)}.currency{font-size:.36em;margin-right:8px;color:#9feaff}.hero-delta{display:inline-flex;margin-top:17px;padding:8px 12px;border-radius:999px;background:rgba(40,229,166,.1);border:1px solid rgba(40,229,166,.22);color:#89ffd5;font-size:13px}.hero-meta{display:flex;gap:18px;flex-wrap:wrap;margin-top:22px}.hero-meta div{min-width:120px}.hero-meta small{display:block;color:var(--muted);font-size:11px}.hero-meta b{display:block;margin-top:5px;font-size:16px}.speed-cluster{position:relative;width:220px;height:220px;display:grid;place-items:center}.speed-cluster svg{position:absolute;inset:0;transform:rotate(-116deg);filter:drop-shadow(0 0 10px rgba(54,214,255,.25))}.speed-cluster circle{fill:none;stroke-linecap:round}.speed-bg{stroke:rgba(113,214,255,.09);stroke-width:14}.speed-value{stroke:url(#speedGradient);stroke-width:14;stroke-dasharray:478;stroke-dashoffset:calc(478 - (478 * var(--meter))/100)}.speed-core{width:135px;height:135px;border-radius:50%;background:radial-gradient(circle,rgba(23,47,72,.92),rgba(5,12,23,.96));border:1px solid rgba(111,218,255,.2);display:grid;place-items:center;text-align:center;box-shadow:inset 0 0 35px rgba(54,214,255,.09),0 0 35px rgba(54,214,255,.08);animation:breathe 3s ease-in-out infinite}.speed-core b{display:block;font-size:37px}.speed-core small{color:var(--muted);font-size:10px}
    .ai-card{padding:24px;min-height:300px;background:radial-gradient(circle at 85% 10%,rgba(139,92,246,.2),transparent 33%),linear-gradient(150deg,rgba(18,14,39,.92),rgba(6,13,25,.95))}.ai-header{display:flex;justify-content:space-between;align-items:center}.ai-orb{width:56px;height:56px;border-radius:50%;display:grid;place-items:center;font:22px var(--title);background:radial-gradient(circle at 35% 30%,#fff 0 3%,#b79cff 8%,#6e42ff 34%,#21134f 70%);box-shadow:0 0 18px rgba(132,91,255,.65),0 0 44px rgba(105,71,255,.35);animation:breathe 2.6s ease-in-out infinite}.ai-live{font-size:11px;color:#c9bfff}.ai-live i{display:inline-block;width:7px;height:7px;border-radius:50%;background:#9b77ff;box-shadow:0 0 12px #9b77ff;margin-right:6px;animation:pulse 1.8s infinite}.ai-card h3{font-size:22px;margin:18px 0 12px}.ai-message{display:grid;gap:10px;color:#dbe6ef;line-height:1.55;font-size:13px}.ai-message div{padding:10px 12px;border-left:2px solid #8f6cff;background:rgba(133,91,255,.065);border-radius:0 10px 10px 0}.ai-actions{display:flex;gap:9px;margin-top:18px}.soft-btn{flex:1;border:1px solid rgba(141,111,255,.25);background:rgba(133,91,255,.1);border-radius:12px;padding:10px;text-align:center;font-size:12px}
    .stats-grid{grid-template-columns:repeat(4,minmax(0,1fr));margin-top:18px}.stat-card{padding:19px}.stat-head{display:flex;justify-content:space-between;align-items:center}.stat-label,.stat-note{color:var(--muted);font-size:12px}.stat-icon{width:35px;height:35px;border-radius:12px;display:grid;place-items:center;background:color-mix(in srgb,var(--tone) 14%,transparent);color:var(--tone)}.stat-value{font-size:23px;font-weight:800;margin-top:15px;white-space:nowrap}.stat-note{font-size:11px;margin-top:6px}.mini-line,.bar{height:5px;background:rgba(255,255,255,.055);border-radius:999px;overflow:hidden}.mini-line{margin-top:15px;height:3px}.mini-line i,.bar i{display:block;height:100%;width:0;border-radius:inherit;transition:width 1.2s ease}.mini-line i{background:linear-gradient(90deg,var(--tone),transparent);box-shadow:0 0 10px var(--tone)}
    .content-grid{grid-template-columns:minmax(0,1.3fr) minmax(340px,.7fr);margin-top:18px}.section-card{padding:23px}.section-title{display:flex;justify-content:space-between;gap:16px;margin-bottom:18px}.section-title h3{font-size:21px;margin:4px 0 0}.section-title small{color:var(--muted)}.chart{height:235px;border-radius:16px;background:linear-gradient(180deg,rgba(54,214,255,.035),transparent);overflow:hidden}.chart svg{width:100%;height:100%}.chart-gridline{stroke:rgba(130,200,230,.09)}.area{fill:url(#areaGradient);opacity:.28}.line{fill:none;stroke:url(#lineGradient);stroke-width:4;stroke-linecap:round;stroke-linejoin:round;filter:drop-shadow(0 0 6px rgba(54,214,255,.5));stroke-dasharray:1200;stroke-dashoffset:1200;animation:draw 1.8s ease forwards .55s}.chart-dot{fill:#dffbff;stroke:var(--accent);stroke-width:4;opacity:0;animation:dotIn .35s ease forwards 1.65s}.chart-legend{display:flex;gap:14px;font-size:11px;color:var(--muted)}.chart-legend span:before{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--dot);margin-right:6px;box-shadow:0 0 9px var(--dot)}.category-list,.bank-list{display:grid;gap:13px}.cat-row{display:grid;grid-template-columns:96px minmax(0,1fr) auto;gap:12px;align-items:center;font-size:12px}.bar{height:7px}.bar i{background:linear-gradient(90deg,var(--bar),transparent);box-shadow:0 0 9px var(--bar)}.bank-row{display:flex;align-items:center;justify-content:space-between;padding:13px 0;border-bottom:1px solid rgba(255,255,255,.055)}.bank-row:last-child{border-bottom:0}.bank-name{display:flex;align-items:center;gap:10px}.bank-dot{width:10px;height:10px;border-radius:50%;background:var(--bank);box-shadow:0 0 12px var(--bank)}
    .gauge-grid{grid-template-columns:repeat(4,minmax(0,1fr));margin-top:18px}.gauge-card{padding:20px;display:grid;place-items:center;text-align:center}.gauge{width:142px;height:142px;border-radius:50%;display:grid;place-items:center;background:conic-gradient(var(--tone) calc(var(--p)*1%),rgba(255,255,255,.055) 0);position:relative;box-shadow:0 0 24px color-mix(in srgb,var(--tone) 18%,transparent);animation:breathe 4s ease-in-out infinite}.gauge:before{content:"";position:absolute;inset:11px;border-radius:50%;background:radial-gradient(circle,rgba(20,35,53,.94),rgba(4,10,19,.98))}.gauge-inner{position:relative}.gauge-inner b{display:block;font-size:25px}.gauge-inner small,.gauge-card p{font-size:10px;color:var(--muted)}.gauge-card p{font-size:11px;margin:12px 0 0}footer{text-align:center;color:#506578;font-size:10px;padding-top:34px}
    .boot{position:fixed;z-index:1000;inset:0;display:grid;place-content:center;justify-items:center;background:radial-gradient(circle,#111c36,#02050b 64%);transition:.55s}.boot.hide{opacity:0;visibility:hidden}.boot-orbit{width:130px;height:130px;border-radius:50%;border:1px solid rgba(54,214,255,.24);display:grid;place-items:center;position:relative;animation:spin 3s linear infinite}.boot-orbit:before,.boot-orbit:after{content:"";position:absolute;inset:10px;border-radius:50%;border:2px solid transparent;border-top-color:var(--accent)}.boot-orbit:after{inset:24px;border-top-color:var(--purple);animation:spin 1.6s linear reverse infinite}.boot-core{width:62px;height:62px;border-radius:50%;display:grid;place-items:center;font:28px var(--title);background:#081426;box-shadow:0 0 28px rgba(54,214,255,.42);animation:breathe 1.6s ease-in-out infinite}.boot h1{font:27px var(--title);margin:22px 0 7px}.boot p{color:#9eb6c9;font-size:12px}.boot-track{width:250px;height:3px;background:rgba(255,255,255,.07);overflow:hidden;margin-top:16px}.boot-track i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--accent),var(--purple),var(--green));animation:bootbar 2.1s ease forwards}
    @keyframes pulse{50%{opacity:.38;transform:scale(.78)}}@keyframes breathe{50%{filter:brightness(1.25);transform:scale(1.018)}}@keyframes scan{0%,72%{left:-80%}92%,100%{left:150%}}@keyframes reveal{to{opacity:1;transform:none}}@keyframes draw{to{stroke-dashoffset:0}}@keyframes dotIn{to{opacity:1}}@keyframes spin{to{transform:rotate(360deg)}}@keyframes bootbar{to{width:100%}}
    @media(max-width:1120px){.app-shell{grid-template-columns:84px 1fr}.brand-text,.nav-item span:last-child,.side-status small{display:none}.brand-block,.nav-item{justify-content:center}.hero-grid{grid-template-columns:1fr}.stats-grid,.gauge-grid{grid-template-columns:repeat(2,1fr)}}
    @media(max-width:760px){.app-shell{display:block}.sidebar,.topbar{display:none}.mobile-header{height:64px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:40;padding:0 15px;background:rgba(3,8,16,.84);backdrop-filter:blur(18px);border-bottom:1px solid var(--line)}.mobile-brand{display:flex;align-items:center;gap:10px}.mobile-brand .brand-mark{width:39px;height:39px;border-radius:13px;font-size:20px}.mobile-actions{display:flex;gap:8px}.mobile-actions .icon-btn{width:37px;height:37px}main{padding:14px 12px calc(96px + env(safe-area-inset-bottom))}.dashboard-grid{gap:12px}.hero-grid{display:block}.hero-card{min-height:315px;padding:22px 18px;display:block}.hero-card h2{font-size:24px}.hero-money{font-size:clamp(37px,11vw,52px);letter-spacing:-2px}.hero-meta{gap:8px;margin-top:17px}.hero-meta div{min-width:calc(50% - 8px);padding:10px;border-radius:12px;background:rgba(255,255,255,.025)}.speed-cluster{position:absolute;right:-37px;top:25px;width:150px;height:150px;opacity:.55}.speed-core{width:89px;height:89px}.speed-core b{font-size:25px}.ai-card{margin-top:12px;padding:20px;min-height:auto}.stats-grid,.gauge-grid{grid-template-columns:repeat(2,1fr);margin-top:12px;gap:12px}.stat-card{padding:15px}.stat-value{font-size:19px}.content-grid{grid-template-columns:1fr;margin-top:12px;gap:12px}.section-card{padding:18px}.chart{height:200px}.gauge-card{padding:15px}.gauge{width:118px;height:118px}.cat-row{grid-template-columns:78px 1fr}.cat-row b{grid-column:2;text-align:right}.panel{border-radius:18px}.mobile-nav{position:fixed;left:9px;right:9px;bottom:calc(8px + env(safe-area-inset-bottom));height:65px;display:grid;grid-template-columns:repeat(5,1fr);padding:6px;border:1px solid var(--line);border-radius:20px;background:rgba(4,10,19,.92);backdrop-filter:blur(22px);z-index:50}.mobile-nav-item{display:grid;place-items:center;align-content:center;gap:2px;color:#738b9e;border-radius:14px}.mobile-nav-item span{font-size:19px}.mobile-nav-item small{font-size:9px}.mobile-nav-item.active{color:#fff;background:linear-gradient(145deg,rgba(54,214,255,.15),rgba(125,86,255,.12))}}
    @media(max-width:380px){main{padding-inline:9px}.hero-money{font-size:36px}.stat-value{font-size:17px}.gauge{width:105px;height:105px}.mobile-nav{left:6px;right:6px}}
    @media(prefers-reduced-motion:reduce){*,*:before,*:after{animation:none!important;transition:none!important}}
    '''
    script = r'''<script>(()=>{document.querySelectorAll('.reveal').forEach((e,i)=>e.style.setProperty('--delay',(i*.055)+'s'));document.querySelectorAll('[data-count]').forEach((e,i)=>{const t=Number(e.dataset.count||0),d=Number(e.dataset.decimals||0),s=e.dataset.suffix||'',st=performance.now()+i*16;function f(n){const p=Math.min(1,Math.max(0,(n-st)/1050)),v=t*(1-Math.pow(1-p,3));e.textContent=v.toLocaleString('zh-TW',{minimumFractionDigits:d,maximumFractionDigits:d})+s;if(p<1)requestAnimationFrame(f)}requestAnimationFrame(f)});setTimeout(()=>document.querySelectorAll('[data-width]').forEach(e=>e.style.width=Math.min(100,Math.max(0,Number(e.dataset.width||0)))+'%'),250);const c=()=>{const d=new Date();document.querySelectorAll('[data-clock]').forEach(e=>e.textContent=d.toLocaleTimeString('zh-TW',{hour:'2-digit',minute:'2-digit'}));document.querySelectorAll('[data-date]').forEach(e=>e.textContent=d.toLocaleDateString('zh-TW',{year:'numeric',month:'long',day:'numeric',weekday:'long'}))};c();setInterval(c,1000);const b=document.getElementById('boot');if(b){const a=['正在啟動超跑財務中控...','正在同步財務資料...','正在校正數位儀表...','AI 財務助理已上線'];let n=0,l=document.getElementById('boot-line'),x=setInterval(()=>{n++;if(n<a.length)l.textContent=a[n]},470);setTimeout(()=>{clearInterval(x);b.classList.add('hide')},2200)}})();</script>'''
    return f'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#03060d"><title>{escape(title)}</title><style>{css}</style></head><body><div class="app-shell"><aside class="sidebar"><div class="brand-block"><div class="brand-mark"><span>財</span></div><div class="brand-text"><b>AI 財務管家</b><small>超跑智慧中控</small></div></div><nav class="nav-stack">{nav}</nav><div class="side-status"><div class="online"><i class="pulse-dot"></i><span>系統正常</span></div><small>財務資料已同步<br>AI 助理待命中</small></div></aside><div class="content-wrap"><header class="topbar"><div class="top-title"><h1>{escape(title)}</h1><p><span data-date>載入日期中</span> · <span data-clock>--:--</span></p></div><div class="top-actions"><div class="status-pill"><i class="pulse-dot"></i>系統正常</div><a class="icon-btn" href="/jarvis/command">✦</a><a class="icon-btn" href="/admin">⚙</a></div></header><header class="mobile-header"><div class="mobile-brand"><div class="brand-mark"><span>財</span></div><div><b>AI 財務管家</b><small>超跑智慧中控</small></div></div><div class="mobile-actions"><a class="icon-btn" href="/jarvis/command">✦</a><a class="icon-btn" href="/admin">⚙</a></div></header><main>{body}<footer>AI 財務管家 4.5 Nebula Ultimate · 電腦與手機雙介面</footer></main></div></div><nav class="mobile-nav">{mobile_nav}</nav>{boot_html}{script}</body></html>'''

@app.route("/jarvis")
def jarvis_garage():
    s=calculate_jarvis_summary();categories=list(s.get("categories") or [])[:6]
    vals=[float(x[1]) for x in categories] if categories else [0]*6;points=_v4_sparkline(vals+[float(s.get("income",0)),float(s.get("expense",0))]);polygon=f"0,150 {points} 520,150"
    dots=''.join(f'<circle class="chart-dot" cx="{p.split(",")[0]}" cy="{p.split(",")[1]}" r="4"/>' for p in points.split())
    advice=s.get("advice") or ["本月財務資料已完成同步。"];advice_html=''.join(f'<div>✦ {escape(str(x))}</div>' for x in advice[:3])
    colors=["#45e6ff","#7b61ff","#28e5a6","#ffc857","#ff9f43"]
    bank_rows=''.join(f'<div class="bank-row"><div class="bank-name"><i class="bank-dot" style="--bank:{colors[i%5]}"></i><span>{escape(n)}</span></div><b>NT$ <span data-count="{int(v.get("balance") or 0)}">0</span></b></div>' for i,(n,v) in enumerate(s.get("banks",{}).items())) or '<p style="color:var(--muted)">尚無銀行資料</p>'
    total=max(sum(vals),1);cat_colors=["#36d6ff","#7b61ff","#28e5a6","#ffc857","#ff5b78","#ff9f43"]
    category_rows=''.join(f'<div class="cat-row"><span>{escape(str(n))}</span><div class="bar"><i style="--bar:{cat_colors[i%6]}" data-width="{_v4_progress(float(v)/total*100):.1f}"></i></div><b>NT$ {_v4_money(v)}</b></div>' for i,(n,v) in enumerate(categories)) or '<p style="color:var(--muted)">本月尚無分類支出</p>'
    def stat(label,value,note,icon,tone,width):return f'<section class="panel stat-card reveal" style="--tone:{tone}"><div class="stat-head"><span class="stat-label">{label}</span><span class="stat-icon">{icon}</span></div><div class="stat-value">NT$ <span data-count="{int(value)}">0</span></div><div class="stat-note">{note}</div><div class="mini-line"><i data-width="{_v4_progress(width):.1f}"></i></div></section>'
    def gauge(label,value,note,tone):
        v=_v4_progress(value);return f'<section class="panel gauge-card reveal" style="--tone:{tone}"><div class="gauge" style="--p:{v:.1f}"><div class="gauge-inner"><b><span data-count="{v:.1f}" data-decimals="1" data-suffix="%">0%</span></b><small>{label}</small></div></div><p>{note}</p></section>'
    cash=float(s.get('cash',0));debt=float(s.get('debt',0));balance=float(s.get('balance',0));income=float(s.get('income',0));expense=float(s.get('expense',0));saving=float(s.get('saving_rate',0));health=float(s.get('health_score',0));debt_ratio=debt/max(cash+debt,1)*100
    body=f'''<div class="dashboard-grid hero-grid"><section class="panel hero-card reveal"><div><div class="eyebrow">財務作戰中心</div><h2>總淨資產</h2><div class="hero-money"><span class="currency">NT$</span><span data-count="{int(s['net_worth'])}">0</span></div><div class="hero-delta">▲ 本月結餘 {'+' if balance>=0 else ''}NT$ <span data-count="{int(balance)}">0</span></div><div class="hero-meta"><div><small>財務健康度</small><b>{int(health)} / 100</b></div><div><small>本月儲蓄率</small><b>{saving:.1f}%</b></div><div><small>風險狀態</small><b>{escape(str(s.get('risk','低')))}風險</b></div><div><small>即時時間</small><b data-clock>--:--</b></div></div></div><div class="speed-cluster" style="--meter:{max(8,min(96,health)):.1f}"><svg viewBox="0 0 180 180"><defs><linearGradient id="speedGradient"><stop offset="0" stop-color="#36d6ff"/><stop offset=".55" stop-color="#7b61ff"/><stop offset="1" stop-color="#28e5a6"/></linearGradient></defs><circle class="speed-bg" cx="90" cy="90" r="76"/><circle class="speed-value" cx="90" cy="90" r="76"/></svg><div class="speed-core"><div><b>{int(health)}</b><small>財務性能</small></div></div></div></section><section class="panel ai-card reveal"><div class="ai-header"><div class="ai-orb">AI</div><span class="ai-live"><i></i>助理已上線</span></div><h3>今日財務簡報</h3><div class="ai-message">{advice_html}</div><div class="ai-actions"><a class="soft-btn" href="/jarvis/command">詢問 AI</a><a class="soft-btn" href="/admin">快速記帳</a></div></section></div>
    <div class="dashboard-grid stats-grid">{stat('本月收入',income,'收入引擎運作正常','＋','#28e5a6',70)}{stat('本月支出',expense,'即時監控消費動態','－','#ff5b78',expense/max(income,1)*100)}{stat('本月結餘',balance,'可運用資金餘額','◇','#ffc857',abs(balance)/max(income,1)*100)}{stat('現金資產',cash,'銀行與現金合計','⌁','#36d6ff',76)}</div>
    <div class="dashboard-grid content-grid"><section class="panel section-card reveal"><div class="section-title"><div><div class="eyebrow">本月現金流</div><h3>收支趨勢</h3></div><div class="chart-legend"><span style="--dot:#36d6ff">即時趨勢</span><span style="--dot:#7b61ff">AI 預估</span></div></div><div class="chart"><svg viewBox="0 0 520 170" preserveAspectRatio="none"><defs><linearGradient id="areaGradient" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#36d6ff"/><stop offset="1" stop-color="transparent"/></linearGradient><linearGradient id="lineGradient"><stop offset="0" stop-color="#36d6ff"/><stop offset=".55" stop-color="#7b61ff"/><stop offset="1" stop-color="#28e5a6"/></linearGradient></defs><path class="chart-gridline" d="M0 40H520 M0 85H520 M0 130H520"/><polygon class="area" points="{polygon}"/><polyline class="line" points="{points}"/>{dots}</svg></div></section><section class="panel section-card reveal"><div class="section-title"><div><div class="eyebrow">消費結構</div><h3>支出分類</h3></div><small>本月</small></div><div class="category-list">{category_rows}</div></section></div>
    <div class="dashboard-grid content-grid"><section class="panel section-card reveal"><div class="section-title"><div><div class="eyebrow">資產配置</div><h3>銀行資產</h3></div><small>即時同步</small></div><div class="bank-list">{bank_rows}</div></section><section class="panel section-card reveal"><div class="section-title"><div><div class="eyebrow">系統摘要</div><h3>財務性能</h3></div><small>超跑中控模式</small></div><div class="bank-list"><div class="bank-row"><span>月底預估支出</span><b>NT$ <span data-count="{int(s.get('projected_expense',0))}">0</span></b></div><div class="bank-row"><span>信用卡已使用</span><b>NT$ <span data-count="{int(s.get('credit_used',0))}">0</span></b></div><div class="bank-row"><span>儲蓄目標完成度</span><b><span data-count="{float(s.get('goal_ratio',0)):.1f}" data-decimals="1" data-suffix="%">0%</span></b></div></div></section></div>
    <div class="dashboard-grid gauge-grid">{gauge('儲蓄率',saving,f'本月結餘 NT$ {_v4_money(balance)}','#ffc857')}{gauge('信用卡使用率',s.get('credit_ratio',0),f'已使用 NT$ {_v4_money(s.get("credit_used",0))}','#ff9f43')}{gauge('負債比例',debt_ratio,f'負債 NT$ {_v4_money(debt)}','#ff5b78')}{gauge('財務健康度',health,f'{s.get("risk","低")}風險','#28e5a6')}</div>'''
    return jarvis_layout(body,"garage","財務總覽",request.args.get("boot","1")=="1")

@app.route("/jarvis/command")
def jarvis_command():
    s = calculate_jarvis_summary()
    missions = [("維持正現金流", s["balance"] >= 0),("信用卡使用率低於 30%", s["credit_ratio"] < 30),("儲蓄率達到 20%", s["saving_rate"] >= 20),("預算尚未超支", s["budget_ratio"] <= 100)]
    mission_html = "".join(f'<div class="row"><span>{"✓" if ok else "△"} {escape(name)}</span><b class="{ "positive" if ok else "negative"}">{"已達成" if ok else "需注意"}</b></div>' for name, ok in missions)
    advice_html = "".join(f'<div class="row"><span>✦ {escape(str(item))}</span></div>' for item in s.get("advice", []))
    body = f'''<div class="grid g2"><section class="panel ai-card"><div class="eyebrow">AI 核心分析</div><h1>財務風險雷達</h1><div class="ring" style="--p:{_v4_progress(s['health_score']):.1f};margin-top:28px"><div class="ring-inner"><b>{int(s['health_score'])}</b><small>健康分數</small></div></div><p class="muted" style="text-align:center">目前判定為「{escape(s['risk'])}風險」</p></section><section class="panel"><div class="eyebrow">本月任務</div><h2>財務目標狀態</h2><div class="list">{mission_html}</div></section></div><section class="panel" style="margin-top:16px"><div class="eyebrow">AI 建議</div><h2>下一步行動</h2><div class="list">{advice_html}</div></section><div class="grid g3" style="margin-top:16px"><section class="panel"><div class="eyebrow">總負債</div><div class="metric negative">NT$ {_v4_money(s['debt'])}</div></section><section class="panel"><div class="eyebrow">預算使用率</div><div class="metric">{s['budget_ratio']:.1f}%</div><div class="bar"><i data-width="{_v4_progress(s['budget_ratio']):.1f}"></i></div></section><section class="panel"><div class="eyebrow">信用卡使用率</div><div class="metric">{s['credit_ratio']:.1f}%</div><div class="bar"><i data-width="{_v4_progress(s['credit_ratio']):.1f}"></i></div></section></div>'''
    return jarvis_layout(body, "command", "AI 財務分析中心")


@app.route("/jarvis/private")
def jarvis_private():
    s = calculate_jarvis_summary()
    rating = "AAA" if s["health_score"] >= 90 else "AA" if s["health_score"] >= 80 else "A" if s["health_score"] >= 70 else "BBB"
    cats = "".join(f'<div class="row"><span>{escape(str(name))}</span><b>NT$ {_v4_money(value)}</b></div>' for name, value in (s.get("categories") or [])[:7]) or '<p class="muted">尚無資料</p>'
    summary = "".join(f'<div class="row"><span>{escape(str(item))}</span></div>' for item in (s.get("advice") or [])[:4])
    body = f'''<section class="panel hero"><div><div class="eyebrow">私人財富報告 · {escape(str(s['month']))}</div><h1 class="title">淨資產總覽</h1><div class="money">NT$ {_v4_money(s['net_worth'])}</div></div><div class="hero-side"><div class="eyebrow">財富評級</div><div class="money accent">{rating}</div></div></section><div class="grid g3" style="margin-top:16px"><section class="panel"><div class="eyebrow">儲蓄率</div><div class="metric positive">{s['saving_rate']:.1f}%</div></section><section class="panel"><div class="eyebrow">本月結餘</div><div class="metric {'positive' if s['balance']>=0 else 'negative'}">NT$ {_v4_money(s['balance'])}</div></section><section class="panel"><div class="eyebrow">資產負債比</div><div class="metric">{(s['debt']/max(s['cash'],1)*100):.1f}%</div></section></div><div class="grid g2" style="margin-top:16px"><section class="panel"><div class="eyebrow">支出組合</div><h2>主要分類</h2>{cats}</section><section class="panel ai-card"><div class="eyebrow">高階摘要</div><h2>本月財務判讀</h2>{summary}</section></div>'''
    return jarvis_layout(body, "private", "私人財富報告")


@app.route("/jarvis/themes")
def jarvis_themes():
    body = '''<section class="panel"><div class="eyebrow">外觀設定</div><h1>選擇你的儀表主題</h1><p class="muted">設定會保存在目前裝置，不會影響資料庫。</p><div class="theme-grid"><button class="theme" data-theme="cyan">◈ 鳳凰青</button><button class="theme" data-theme="blue">◉ 指揮藍</button><button class="theme" data-theme="red">◆ 性能紅</button><button class="theme" data-theme="gold">✦ 尊爵金</button></div></section><section class="panel" style="margin-top:16px"><div class="eyebrow">4.0 Alpha 狀態</div><h2>已完成項目</h2><div class="list"><div class="row"><span>全新財務作戰中心</span><b class="positive">完成</b></div><div class="row"><span>即時日期與時間</span><b class="positive">完成</b></div><div class="row"><span>AI 今日財務摘要</span><b class="positive">完成</b></div><div class="row"><span>動態收支圖表</span><b class="positive">完成</b></div><div class="row"><span>手機底部導覽</span><b class="positive">完成</b></div><div class="row"><span>既有後端與資料表</span><b class="positive">保留</b></div></div></section>'''
    return jarvis_layout(body, "themes", "4.0 外觀設定")


@app.route("/api/jarvis/summary")
def jarvis_summary_api():
    return jsonify(calculate_jarvis_summary())

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

    if user_text in {"查看上一筆", "上一筆"}:
        try:
            item = get_latest_transaction(user_id)
        except Exception as error:
            print("查看上一筆失敗：", error)
            reply_line(event, "查看上一筆失敗，請稍後再試。")
            return

        if not item:
            reply_line(event, "目前沒有可查看的記帳資料。")
            return

        reply_line(
            event,
            "🧾 上一筆記帳\n"
            f"帳戶：{normalize_account(item.get('account'))}\n"
            f"類型：{item.get('type')}\n"
            f"分類：{item.get('category')}\n"
            f"項目：{item.get('description')}\n"
            f"金額：NT$ {int(item.get('amount') or 0):,}",
        )
        return

    edit_last_full_match = re.match(
        r"^修改上一筆\s+(.+?)\s+(\d[\d,]*)\s*$",
        user_text,
    )
    if edit_last_full_match:
        description = edit_last_full_match.group(1).strip()
        amount = parse_positive_int(edit_last_full_match.group(2))
        try:
            item = get_latest_transaction(user_id)
            if not item:
                reply_line(event, "目前沒有可修改的記帳資料。")
                return
            transaction_type = str(item.get("type") or "支出")
            category = (
                classify_income(description)
                if transaction_type == "收入"
                else classify_expense(description)
            )
            update_transaction_record(
                item.get("id"),
                description=description,
                amount=amount,
                category=category,
            )
        except Exception as error:
            print("修改上一筆失敗：", error)
            reply_line(event, f"修改上一筆失敗：{error}")
            return

        reply_line(
            event,
            f"✅ 已修改上一筆\n項目：{description}\n金額：NT$ {amount:,}",
        )
        return

    edit_last_amount_match = re.match(
        r"^修改上一筆金額\s+(\d[\d,]*)\s*$",
        user_text,
    )
    if edit_last_amount_match:
        amount = parse_positive_int(edit_last_amount_match.group(1))
        try:
            item = get_latest_transaction(user_id)
            if not item:
                reply_line(event, "目前沒有可修改的記帳資料。")
                return
            update_transaction_record(item.get("id"), amount=amount)
        except Exception as error:
            print("修改上一筆金額失敗：", error)
            reply_line(event, f"修改失敗：{error}")
            return

        reply_line(event, f"✅ 上一筆金額已修改為 NT$ {amount:,}")
        return

    edit_last_desc_match = re.match(
        r"^修改上一筆項目\s+(.+?)\s*$",
        user_text,
    )
    if edit_last_desc_match:
        description = edit_last_desc_match.group(1).strip()
        try:
            item = get_latest_transaction(user_id)
            if not item:
                reply_line(event, "目前沒有可修改的記帳資料。")
                return
            transaction_type = str(item.get("type") or "支出")
            category = (
                classify_income(description)
                if transaction_type == "收入"
                else classify_expense(description)
            )
            update_transaction_record(
                item.get("id"),
                description=description,
                category=category,
            )
        except Exception as error:
            print("修改上一筆項目失敗：", error)
            reply_line(event, f"修改失敗：{error}")
            return

        reply_line(event, f"✅ 上一筆項目已修改為：{description}")
        return

    edit_last_category_match = re.match(
        r"^修改上一筆分類\s+(.+?)\s*$",
        user_text,
    )
    if edit_last_category_match:
        category = edit_last_category_match.group(1).strip()
        try:
            item = get_latest_transaction(user_id)
            if not item:
                reply_line(event, "目前沒有可修改的記帳資料。")
                return
            update_transaction_record(item.get("id"), category=category)
        except Exception as error:
            print("修改上一筆分類失敗：", error)
            reply_line(event, f"修改失敗：{error}")
            return

        reply_line(event, f"✅ 上一筆分類已修改為：{category}")
        return

    if user_text == "刪除上一筆":
        try:
            item = get_latest_transaction(user_id)
        except Exception as error:
            print("準備刪除上一筆失敗：", error)
            reply_line(event, "讀取上一筆失敗，請稍後再試。")
            return

        if not item:
            reply_line(event, "目前沒有可刪除的記帳資料。")
            return

        session_key = f"pending_delete_{user_id}"
        app.config[session_key] = {
            "id": item.get("id"),
            "description": item.get("description"),
            "amount": int(item.get("amount") or 0),
        }
        reply_line(
            event,
            "⚠️ 準備刪除上一筆\n"
            f"項目：{item.get('description')}\n"
            f"金額：NT$ {int(item.get('amount') or 0):,}\n\n"
            "請輸入「確認刪除」完成刪除；輸入「取消刪除」取消。",
        )
        return

    if user_text == "取消刪除":
        app.config.pop(f"pending_delete_{user_id}", None)
        reply_line(event, "已取消刪除。")
        return

    if user_text == "確認刪除":
        pending = app.config.pop(f"pending_delete_{user_id}", None)
        if not pending:
            reply_line(event, "目前沒有待確認的刪除操作。")
            return

        try:
            delete_transaction_record(pending["id"])
        except Exception as error:
            print("確認刪除失敗：", error)
            reply_line(event, "刪除失敗，請稍後再試。")
            return

        reply_line(
            event,
            f"✅ 已刪除：{pending['description']} NT$ {pending['amount']:,}",
        )
        return

    simple_bank_match = re.match(
        r"^(玉山|中信|中國信託|渣打|華南|LINE\s*Bank|LINE\s*Pay\s*Money|王道)"
        r"\s*([+\-*/].+)$",
        user_text,
        re.IGNORECASE,
    )
    if simple_bank_match:
        bank_name = normalize_bank_name(simple_bank_match.group(1))
        expression = simple_bank_match.group(2).strip()
        owner = "金家" if bank_name == "王道銀行" else "個人"
        try:
            current_balance = int(
                get_bank_balances(owner)
                .get(bank_name, {})
                .get("balance", 0)
                or 0
            )
            new_balance = safe_eval_number_expression(expression, current_balance)
            set_bank_balance(owner, bank_name, new_balance)
            write_audit_log(
                action="update",
                source="LINE",
                entity_type="bank_balance",
                entity_id=f"{owner}:{bank_name}",
                before_data={"balance": current_balance},
                after_data={"balance": new_balance, "expression": expression},
            )
        except Exception as error:
            reply_line(event, f"銀行餘額調整失敗：{error}")
            return

        reply_line(
            event,
            f"✅ {bank_name}已更新\n"
            f"原餘額：NT$ {current_balance:,}\n"
            f"運算：{expression}\n"
            f"新餘額：NT$ {new_balance:,}",
        )
        return

    simple_card_match = re.match(
        r"^(玉山卡|中信卡|兆豐卡)\s+"
        r"(結帳日|繳款日|應繳|已繳|未繳)"
        r"(?:\s+(\d[\d,]*))?\s*$",
        user_text,
    )
    if simple_card_match:
        card_alias = simple_card_match.group(1)
        action = simple_card_match.group(2)
        value_text = simple_card_match.group(3)
        card_name = {
            "玉山卡": "玉山信用卡",
            "中信卡": "中信信用卡",
            "兆豐卡": "兆豐信用卡",
        }[card_alias]

        try:
            if action in {"結帳日", "繳款日", "應繳"} and not value_text:
                raise ValueError("請輸入數字")
            if action == "結帳日":
                set_credit_card_values(card_name, statement_day=int(value_text.replace(",", "")))
            elif action == "繳款日":
                set_credit_card_values(card_name, due_day=int(value_text.replace(",", "")))
            elif action == "應繳":
                set_credit_card_values(card_name, statement_amount=int(value_text.replace(",", "")))
            elif action == "已繳":
                set_credit_card_values(card_name, payment_status="已繳交")
            elif action == "未繳":
                set_credit_card_values(card_name, payment_status="未繳交")
        except Exception as error:
            reply_line(event, f"信用卡更新失敗：{error}")
            return

        reply_line(event, f"✅ {card_name}「{action}」已更新")
        return

    if user_text in {"智慧分析", "本月分析", "財務分析"}:
        summary = calculate_smart_summary()
        reply_line(
            event,
            f"📊 {summary['month']} 智慧分析\n"
            f"收入：NT$ {int(summary['income']):,}\n"
            f"支出：NT$ {int(summary['expense']):,}\n"
            f"結餘：NT$ {int(summary['balance']):,}\n"
            f"儲蓄率：{summary['saving_rate']:.1f}%\n"
            f"月底預估支出：NT$ {int(summary['projected_expense']):,}\n"
            f"最大分類：{summary['top_category']}\n\n"
            + "\n".join(f"• {text}" for text in summary["advice"][:3]),
        )
        return

    budget_match = re.match(r"^設定預算\s+(\S+)\s+([\d,]+)$", user_text)
    if budget_match:
        category = budget_match.group(1)
        amount = int(budget_match.group(2).replace(",", ""))
        month = datetime.now(TAIPEI).strftime("%Y-%m")
        try:
            rows = supabase.table("budgets").select("id").eq("category", category).eq("month", month).limit(1).execute().data or []
            payload = {"category": category, "month": month, "amount": amount, "updated_at": datetime.now(TAIPEI).isoformat()}
            if rows:
                supabase.table("budgets").update(payload).eq("id", rows[0]["id"]).execute()
            else:
                supabase.table("budgets").insert(payload).execute()
            reply_line(event, f"✅ {month} {category}預算已設定為 NT$ {amount:,}")
        except Exception as error:
            reply_line(event, f"設定預算失敗：{error}")
        return

    goal_match = re.match(r"^新增目標\s+(\S+)\s+([\d,]+)$", user_text)
    if goal_match:
        name = goal_match.group(1)
        target = int(goal_match.group(2).replace(",", ""))
        try:
            supabase.table("saving_goals").insert({"name": name, "target_amount": target, "current_amount": 0, "updated_at": datetime.now(TAIPEI).isoformat()}).execute()
            reply_line(event, f"✅ 儲蓄目標「{name}」已建立，目標 NT$ {target:,}")
        except Exception as error:
            reply_line(event, f"新增目標失敗：{error}")
        return

    goal_deposit_match = re.match(r"^目標存款\s+(\S+)\s+([+\-*/]?[\d,]+)$", user_text)
    if goal_deposit_match:
        name = goal_deposit_match.group(1)
        expression = goal_deposit_match.group(2)
        try:
            rows = supabase.table("saving_goals").select("*").eq("name", name).limit(1).execute().data or []
            if not rows:
                raise ValueError("找不到目標")
            row = rows[0]
            current = int(row.get("current_amount") or 0)
            new_value = safe_eval_number_expression(expression, current)
            supabase.table("saving_goals").update({"current_amount": new_value, "updated_at": datetime.now(TAIPEI).isoformat()}).eq("id", row["id"]).execute()
            reply_line(event, f"✅ {name}目前已存 NT$ {new_value:,}")
        except Exception as error:
            reply_line(event, f"更新目標失敗：{error}")
        return

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
            "快速銀行：玉山 +500、王道 -1000\n"
            "快速信用卡：玉山卡 結帳日 5\n"
            "快速信用卡：玉山卡 繳款日 20\n"
            "快速信用卡：玉山卡 應繳 12500\n"
            "快速信用卡：玉山卡 已繳\n"
            "查看上一筆：查看上一筆\n"
            "修改上一筆：修改上一筆 午餐 150\n"
            "修改金額：修改上一筆金額 150\n"
            "修改項目：修改上一筆項目 午餐\n"
            "修改分類：修改上一筆分類 飲食\n"
            "刪除上一筆：刪除上一筆\n"
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
