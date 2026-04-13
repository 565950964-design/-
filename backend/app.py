"""
记账小助手 - Flask 后端
支持自然语言解析记账、账单查询、统计分析
"""

import re
import os
from datetime import datetime, timedelta
import csv
import io
from flask import Flask, request, jsonify, send_from_directory, redirect
from flask_cors import CORS
import sqlite3

try:
    from wechat_handler import register_wechat_routes
except Exception:
    register_wechat_routes = None

app = Flask(__name__, static_folder="../frontend", static_url_path="")

DB_PATH = os.path.join(os.path.dirname(__file__), "../data/bills.db")
DEFAULT_USER_ID = "web-local"
ADMIN_WEB_TOKEN = os.getenv("ADMIN_WEB_TOKEN", "")
USER_WEB_TOKEN = os.getenv("USER_WEB_TOKEN", "")
SECURITY_MODE = os.getenv("SECURITY_MODE", "compat").strip().lower()
STRICT_SECURITY = SECURITY_MODE == "strict"
CORS_ALLOW_ORIGINS = [item.strip() for item in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if item.strip()]
APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("PORT", os.getenv("APP_PORT", "5000")))
APP_DEBUG = os.getenv("FLASK_DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}
FORCE_HTTPS = os.getenv("FORCE_HTTPS", "0").strip().lower() in {"1", "true", "yes", "on"}

if CORS_ALLOW_ORIGINS:
    CORS(app, resources={r"/api/*": {"origins": CORS_ALLOW_ORIGINS}})

# ==================== 数据库初始化 ====================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn, table_name, column_name, column_definition):
    columns = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    existing_names = {column[1] for column in columns}
    if column_name not in existing_names:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")


def migrate_budgets_table(conn):
    columns = conn.execute("PRAGMA table_info(budgets)").fetchall()
    column_names = [column[1] for column in columns]
    if "user_id" in column_names:
        return

    conn.execute("ALTER TABLE budgets RENAME TO budgets_legacy")
    conn.execute(
        """
        CREATE TABLE budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT 'web-local',
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            amount REAL NOT NULL,
            UNIQUE(user_id, year, month)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO budgets (id, user_id, year, month, amount)
        SELECT id, 'web-local', year, month, amount
        FROM budgets_legacy
        """
    )
    conn.execute("DROP TABLE budgets_legacy")


def get_request_user_id():
    return request.args.get("user_id") or DEFAULT_USER_ID


def require_admin_auth():
    """管理接口鉴权：未配置口令时放行，配置后需携带 X-Admin-Token。"""
    if not ADMIN_WEB_TOKEN:
        return None
    incoming = request.headers.get("X-Admin-Token") or ""
    if incoming != ADMIN_WEB_TOKEN:
        return jsonify({"success": False, "message": "管理员鉴权失败"}), 401
    return None


def require_user_auth():
    """用户接口鉴权：strict 模式启用后要求 X-User-Token。"""
    if not STRICT_SECURITY:
        return None
    if not USER_WEB_TOKEN:
        return jsonify({"success": False, "message": "服务端未配置 USER_WEB_TOKEN"}), 500

    incoming = (
        request.headers.get("X-User-Token")
        or request.args.get("user_token")
        or ((request.get_json(silent=True) or {}).get("user_token"))
        or ""
    )
    if incoming != USER_WEB_TOKEN:
        return jsonify({"success": False, "message": "用户鉴权失败"}), 401
    return None


@app.after_request
def apply_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "font-src 'self' https://cdn.jsdelivr.net data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'"
    )
    return resp


@app.before_request
def enforce_https_if_enabled():
    if not FORCE_HTTPS:
        return None
    if request.is_secure:
        return None
    forwarded_proto = (request.headers.get("X-Forwarded-Proto") or "").lower()
    if forwarded_proto == "https":
        return None
    https_url = request.url.replace("http://", "https://", 1)
    return redirect(https_url, code=301)


def write_approval_log(conn, user_id, action, operator, channel, note=""):
    conn.execute(
        """
        INSERT INTO approval_logs (user_id, action, operator, channel, note, created_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (user_id, action, operator, channel, note),
    )

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT 'web-local',
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            description TEXT NOT NULL,
            bill_type TEXT NOT NULL DEFAULT 'expense',
            created_at TEXT NOT NULL,
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            day INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT 'web-local',
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            amount REAL NOT NULL,
            UNIQUE(user_id, year, month)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wechat_users (
            user_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'pending',
            display_name TEXT NOT NULL DEFAULT '',
            apply_nickname TEXT NOT NULL DEFAULT '',
            apply_contact_tail TEXT NOT NULL DEFAULT '',
            requested_note TEXT NOT NULL DEFAULT '',
            requested_at TEXT NOT NULL,
            approved_at TEXT,
            approved_by TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS approval_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            action TEXT NOT NULL,
            operator TEXT NOT NULL,
            channel TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS undo_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            description TEXT NOT NULL,
            bill_type TEXT NOT NULL,
            source_created_at TEXT NOT NULL,
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            day INTEGER NOT NULL,
            deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    ensure_column(conn, "bills", "user_id", "TEXT NOT NULL DEFAULT 'web-local'")
    ensure_column(conn, "wechat_users", "display_name", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "wechat_users", "apply_nickname", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "wechat_users", "apply_contact_tail", "TEXT NOT NULL DEFAULT ''")
    migrate_budgets_table(conn)
    conn.execute("UPDATE bills SET user_id=? WHERE user_id IS NULL OR user_id=''", (DEFAULT_USER_ID,))
    conn.commit()
    conn.close()

# ==================== 自然语言解析 ====================

CATEGORY_KEYWORDS = {
    "餐饮": ["吃饭", "餐", "饭", "外卖", "早餐", "午餐", "晚餐", "宵夜", "奶茶", "咖啡",
              "火锅", "烧烤", "零食", "饮料", "点心", "小吃", "面条", "米饭"],
    "交通": ["打车", "滴滴", "出租", "地铁", "公交", "高铁", "火车", "飞机", "机票",
              "加油", "停车", "过路费", "共享单车", "骑车"],
    "购物": ["买", "购", "衣服", "鞋", "包", "化妆品", "护肤", "日用品", "超市",
              "网购", "淘宝", "京东", "拼多多", "电商"],
    "娱乐": ["电影", "游戏", "KTV", "唱歌", "玩", "娱乐", "旅游", "景区", "门票",
              "健身", "运动", "游泳", "跑步"],
    "居家": ["房租", "租金", "水电", "煤气", "物业", "宽带", "网费", "家具", "装修"],
    "医疗": ["医院", "药", "看病", "体检", "医疗", "诊所"],
    "教育": ["书", "课程", "培训", "学费", "教育", "学习", "补课"],
    "人情": ["红包", "礼物", "礼金", "份子钱", "请客", "聚餐", "人情"],
    "收入": ["工资", "薪资", "奖金", "收入", "报销", "退款", "返现", "兼职", "收到"],
    "其他": []
}

INCOME_KEYWORDS = ["收入", "工资", "薪资", "奖金", "报销", "退款", "返现", "收到", "到账"]
SPLIT_HINT_WORDS = ["早餐", "早饭", "中饭", "午饭", "午餐", "晚饭", "晚餐", "宵夜", "上午", "中午", "下午", "晚上"]


def extract_primary_amount(text):
    """提取最可能的金额，尽量忽略日期/月份等数字。"""
    candidates = []

    for match in re.finditer(r'[¥￥]\s*(\d+(?:\.\d+)?)', text):
        candidates.append(float(match.group(1)))

    for match in re.finditer(r'(\d+(?:\.\d+)?)\s*(?:块钱|块|元钱|元|RMB)', text, flags=re.IGNORECASE):
        candidates.append(float(match.group(1)))

    for match in re.finditer(r'(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])', text):
        tail = text[match.end():match.end() + 1]
        if tail in {"年", "月", "日", "号"}:
            continue
        candidates.append(float(match.group(1)))

    return candidates[-1] if candidates else None


def split_bill_entries(text):
    amount_matches = list(re.finditer(r'[¥￥]\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*(?:块钱|块|元钱|元|RMB)|(?<![\d.])\d+(?:\.\d+)?(?![\d.])', text, flags=re.IGNORECASE))
    if len(amount_matches) <= 1:
        return []

    segments = []
    start = 0
    for index, match in enumerate(amount_matches):
        end = match.end()
        boundary = end
        while boundary < len(text) and text[boundary] in " ，,；;、/":
            boundary += 1

        if index + 1 < len(amount_matches):
            next_start = amount_matches[index + 1].start()
            segment = text[start:next_start].strip(" ，,；;、/")
            if segment:
                segments.append(segment)
            start = next_start
        else:
            segment = text[start:].strip(" ，,；;、/")
            if segment:
                segments.append(segment)

    parsed_segments = [seg for seg in segments if extract_primary_amount(seg) is not None]
    if len(parsed_segments) >= 2:
        return parsed_segments

    if any(word in text for word in SPLIT_HINT_WORDS):
        fallback_segments = []
        pattern = r'([^，,；;。]+?(?:[¥￥]\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*(?:块钱|块|元钱|元|RMB)|(?<![\d.])\d+(?:\.\d+)?(?![\d.])))'
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            segment = match.group(1).strip(" ，,；;、/")
            if segment and extract_primary_amount(segment) is not None:
                fallback_segments.append(segment)
        if len(fallback_segments) >= 2:
            return fallback_segments
    return []


def parse_budget_command(text, now):
    if "预算" not in text:
        return None

    if any(kw in text for kw in ["查询", "看看", "多少", "剩余", "还剩", "进度"]):
        return None

    amount = extract_primary_amount(text)
    if amount is None or amount <= 0:
        return None

    ym = extract_year_month_query(text, now)
    if ym:
        start_date, _, _ = ym
        return start_date.year, start_date.month, amount
    return now.year, now.month, amount

def parse_bill(text):
    """解析自然语言账单，返回 (amount, category, description, bill_type)"""
    text = text.strip()

    amount = extract_primary_amount(text)

    if amount is None:
        return None

    # 判断收入还是支出
    bill_type = "income" if any(kw in text for kw in INCOME_KEYWORDS) else "expense"

    # 识别分类
    category = "其他"
    best_match_count = 0
    for cat, keywords in CATEGORY_KEYWORDS.items():
        count = sum(1 for kw in keywords if kw in text)
        if count > best_match_count:
            best_match_count = count
            category = cat

    # 收入类型修正
    if bill_type == "income":
        category = "收入"

    # 生成描述（去除金额部分，保留关键词）
    description = re.sub(r'[¥￥]\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*(?:块钱|块|元钱|元|RMB)|(?<![\d.])\d+(?:\.\d+)?(?![\d.])', '', text).strip(" ，,；;、/")
    if not description:
        description = text

    return amount, category, description, bill_type


def is_query_intent(text):
    keywords = ["查询", "查", "看看", "统计", "汇总", "分析", "花了", "消费", "支出", "收入", "多少", "构成", "占比"]
    return any(kw in text for kw in keywords)


def extract_year_month_query(text, now):
    match = re.search(r"(?:(\d{4})\s*年)?\s*(\d{1,2})\s*月", text)
    if not match:
        return None
    year = int(match.group(1)) if match.group(1) else now.year
    month = int(match.group(2))
    if month < 1 or month > 12:
        return None
    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year + 1, 1, 1) - timedelta(days=1)
    else:
        end = datetime(year, month + 1, 1) - timedelta(days=1)
    return start, end, f"{year}年{month}月"


def parse_compact_date(date_str, fallback_year):
    full = re.match(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日|号)?", date_str)
    if full:
        return datetime(int(full.group(1)), int(full.group(2)), int(full.group(3)))

    md = re.match(r"(\d{1,2})[-/.月](\d{1,2})(?:日|号)?", date_str)
    if md:
        return datetime(fallback_year, int(md.group(1)), int(md.group(2)))
    return None


def extract_date_range_query(text, now):
    range_match = re.search(
        r"([\d年月日号./-]+)\s*(?:到|至|~|－|—|-)\s*([\d年月日号./-]+)",
        text,
    )
    if not range_match:
        return None
    left = parse_compact_date(range_match.group(1).strip(), now.year)
    right = parse_compact_date(range_match.group(2).strip(), now.year)
    if not left or not right:
        return None
    if left > right:
        left, right = right, left
    label = f"{left.month}月{left.day}日~{right.month}月{right.day}日"
    if left.year != now.year or right.year != now.year:
        label = f"{left.year}-{left.month:02d}-{left.day:02d} ~ {right.year}-{right.month:02d}-{right.day:02d}"
    return left, right, label


def build_period_summary_reply(conn, user_id, start_date, end_date, label):
    start_key = start_date.year * 10000 + start_date.month * 100 + start_date.day
    end_key = end_date.year * 10000 + end_date.month * 100 + end_date.day

    summary_row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN bill_type='expense' THEN amount ELSE 0 END) AS expense,
            SUM(CASE WHEN bill_type='income' THEN amount ELSE 0 END) AS income,
            COUNT(*) AS cnt
        FROM bills
        WHERE user_id=? AND (year*10000 + month*100 + day) BETWEEN ? AND ?
        """,
        (user_id, start_key, end_key),
    ).fetchone()

    cat_rows = conn.execute(
        """
        SELECT category, SUM(amount) AS total
        FROM bills
        WHERE user_id=?
          AND bill_type='expense'
          AND (year*10000 + month*100 + day) BETWEEN ? AND ?
        GROUP BY category
        ORDER BY total DESC
        """,
        (user_id, start_key, end_key),
    ).fetchall()

    expense = summary_row["expense"] or 0
    income = summary_row["income"] or 0
    count = summary_row["cnt"] or 0

    lines = [f"📊 {label} 统计"]
    lines.append(f"💸 总支出：¥{expense:.2f}")
    lines.append(f"💰 总收入：¥{income:.2f}")
    lines.append(f"💎 结余：¥{income - expense:.2f}")
    lines.append(f"📝 记录笔数：{count}笔")

    if cat_rows:
        lines.append("\n📌 支出构成：")
        for row in cat_rows[:6]:
            ratio = (row["total"] / expense * 100) if expense > 0 else 0
            lines.append(f"  {row['category']}：¥{row['total']:.2f}（{ratio:.1f}%）")
    else:
        lines.append("\n📌 支出构成：暂无支出记录")
    return "\n".join(lines)


def build_keyword_summary_reply(conn, user_id, keyword, start_date, end_date, label):
    start_key = start_date.year * 10000 + start_date.month * 100 + start_date.day
    end_key = end_date.year * 10000 + end_date.month * 100 + end_date.day
    keyword_like = f"%{keyword}%"

    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN bill_type='expense' THEN amount ELSE 0 END) AS expense,
            SUM(CASE WHEN bill_type='income' THEN amount ELSE 0 END) AS income,
            COUNT(*) AS cnt
        FROM bills
        WHERE user_id=?
          AND (year*10000 + month*100 + day) BETWEEN ? AND ?
          AND (description LIKE ? OR category LIKE ?)
        """,
        (user_id, start_key, end_key, keyword_like, keyword_like),
    ).fetchone()

    expense = row["expense"] or 0
    income = row["income"] or 0
    cnt = row["cnt"] or 0
    lines = [f"🔎 关键词“{keyword}”在{label}的统计"]
    lines.append(f"📝 命中笔数：{cnt}笔")
    lines.append(f"💸 支出：¥{expense:.2f}")
    lines.append(f"💰 收入：¥{income:.2f}")
    lines.append(f"💎 净额：¥{income - expense:.2f}")
    return "\n".join(lines)


def build_week_trend_reply(conn, user_id, start_date, end_date, label):
    start_key = start_date.year * 10000 + start_date.month * 100 + start_date.day
    end_key = end_date.year * 10000 + end_date.month * 100 + end_date.day
    rows = conn.execute(
        """
        SELECT year, month, day,
               SUM(CASE WHEN bill_type='expense' THEN amount ELSE 0 END) AS expense,
               SUM(CASE WHEN bill_type='income' THEN amount ELSE 0 END) AS income
        FROM bills
        WHERE user_id=?
          AND (year*10000 + month*100 + day) BETWEEN ? AND ?
        GROUP BY year, month, day
        ORDER BY year, month, day
        """,
        (user_id, start_key, end_key),
    ).fetchall()

    daily_map = {(r["year"], r["month"], r["day"]): r for r in rows}
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    lines = [f"📈 {label} 每日趋势"]
    total_expense = 0
    total_income = 0
    best_day = (None, -1.0)

    cur = start_date
    while cur <= end_date:
        rec = daily_map.get((cur.year, cur.month, cur.day))
        exp = (rec["expense"] if rec else 0) or 0
        inc = (rec["income"] if rec else 0) or 0
        total_expense += exp
        total_income += inc
        if exp > best_day[1]:
            best_day = (cur, exp)

        wk = weekday_names[cur.weekday()]
        lines.append(f"{wk} {cur.month}/{cur.day}：支出¥{exp:.2f}，收入¥{inc:.2f}")
        cur += timedelta(days=1)

    lines.append(f"\n💸 合计支出：¥{total_expense:.2f}")
    lines.append(f"💰 合计收入：¥{total_income:.2f}")
    if best_day[0] is not None and best_day[1] > 0:
        lines.append(f"🔥 支出最高：{best_day[0].month}/{best_day[0].day}（¥{best_day[1]:.2f}）")
    return "\n".join(lines)


def build_visualization_link(user_id):
    base_url = os.getenv("WEB_BASE_URL", "").strip()
    if not base_url:
        base_url = request.url_root.rstrip("/")
    return f"{base_url}/?user_id={user_id}"

# ==================== API 路由 ====================

@app.route("/")
def index():
    return send_from_directory("../frontend", "index.html")

@app.route("/api/chat", methods=["POST"])
def chat():
    """处理自然语言记账消息"""
    auth_error = require_user_auth()
    if auth_error:
        return auth_error

    data = request.get_json()
    message = data.get("message", "").strip()
    user_id = data.get("user_id") or DEFAULT_USER_ID
    if not message:
        return jsonify({"success": False, "reply": "请输入记账内容"})

    # 支持误发快速撤销：删除当前用户最新一条账单
    if message in {"撤销", "撤回", "撤销上一笔", "删除上一笔"}:
        conn_undo = get_db()
        latest = conn_undo.execute(
            "SELECT id, category, amount, description, bill_type FROM bills WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if not latest:
            conn_undo.close()
            return jsonify({"success": False, "reply": "当前账本没有可撤销的记录。", "type": "undo"})
        source = conn_undo.execute(
            "SELECT amount, category, description, bill_type, created_at, year, month, day FROM bills WHERE id=? AND user_id=?",
            (latest["id"], user_id),
        ).fetchone()
        conn_undo.execute(
            """
            INSERT INTO undo_actions (user_id, amount, category, description, bill_type, source_created_at, year, month, day)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                source["amount"],
                source["category"],
                source["description"],
                source["bill_type"],
                source["created_at"],
                source["year"],
                source["month"],
                source["day"],
            ),
        )
        conn_undo.execute("DELETE FROM bills WHERE id=? AND user_id=?", (latest["id"], user_id))
        conn_undo.commit()
        conn_undo.close()

        type_text = "收入" if latest["bill_type"] == "income" else "支出"
        reply = f"🗑️ 已撤销上一笔{type_text}：{latest['category']} ¥{latest['amount']:.2f}（{latest['description']}）\n可发送“恢复上一笔”找回。"
        return jsonify({"success": True, "reply": reply, "type": "undo", "deleted_bill_id": latest["id"]})

    if message in {"恢复", "恢复上一笔", "恢复删除", "找回上一笔"}:
        conn_restore = get_db()
        latest_undo = conn_restore.execute(
            """
            SELECT id, amount, category, description, bill_type, source_created_at, year, month, day
            FROM undo_actions
            WHERE user_id=?
            ORDER BY deleted_at DESC, id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        if not latest_undo:
            conn_restore.close()
            return jsonify({"success": False, "reply": "没有可恢复的记录。", "type": "restore"})

        cursor = conn_restore.execute(
            "INSERT INTO bills (user_id, amount, category, description, bill_type, created_at, year, month, day) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                user_id,
                latest_undo["amount"],
                latest_undo["category"],
                latest_undo["description"],
                latest_undo["bill_type"],
                latest_undo["source_created_at"],
                latest_undo["year"],
                latest_undo["month"],
                latest_undo["day"],
            ),
        )
        restored_id = cursor.lastrowid
        conn_restore.execute("DELETE FROM undo_actions WHERE id=?", (latest_undo["id"],))
        conn_restore.commit()
        conn_restore.close()
        return jsonify({
            "success": True,
            "reply": f"♻️ 已恢复上一笔：{latest_undo['category']} ¥{latest_undo['amount']:.2f}（{latest_undo['description']}）",
            "type": "restore",
            "bill": {"id": restored_id},
        })

    if message in {"帮助", "help", "?", "？"}:
        return jsonify({
            "success": True,
            "type": "help",
            "reply": (
                "📖 使用指南\n"
                "记账：吃饭15块 / 打车20元 / 工资8000\n"
                "查询：今天 / 昨天 / 本月 / 上月 / 上周 / 3月\n"
                "区间：3月1日到3月15日消费\n"
                "关键词：查滴滴 / 统计星巴克\n"
                "趋势：本周趋势\n"
                "预算：这个月预算1800 / 3月预算2000\n"
                "可视化：可视化（返回网页链接）\n"
                "纠错：撤销上一笔 / 恢复上一笔"
            ),
        })

    if any(kw in message for kw in ["可视化", "图表", "趋势图", "仪表盘", "看图", "看报表", "看网页"]):
        link = build_visualization_link(user_id)
        return jsonify({
            "success": True,
            "reply": f"📈 可视化看板链接：\n{link}\n\n打开后可查看分类占比、趋势图、预算进度等。",
            "type": "query",
            "link": link,
        })

    now = datetime.now()
    budget_command = parse_budget_command(message, now)
    if budget_command:
        year, month, amount = budget_command
        conn_budget = get_db()
        existing = conn_budget.execute(
            "SELECT id FROM budgets WHERE user_id=? AND year=? AND month=?",
            (user_id, year, month),
        ).fetchone()
        if existing:
            conn_budget.execute(
                "UPDATE budgets SET amount=? WHERE id=?",
                (amount, existing["id"]),
            )
        else:
            conn_budget.execute(
                "INSERT INTO budgets (user_id, year, month, amount) VALUES (?,?,?,?)",
                (user_id, year, month, amount),
            )
        conn_budget.commit()
        conn_budget.close()
        return jsonify({
            "success": True,
            "type": "budget",
            "reply": f"🎯 已设置 {year}年{month}月预算：¥{amount:.2f}",
        })

    if any(kw in message for kw in ["上周", "上星期"]):
        start_this_week = now - timedelta(days=now.weekday())
        start_last_week = start_this_week - timedelta(days=7)
        end_last_week = start_this_week - timedelta(days=1)
        conn_q = get_db()
        reply = build_period_summary_reply(conn_q, user_id, start_last_week, end_last_week, "上周")
        conn_q.close()
        return jsonify({"success": True, "reply": reply, "type": "query"})

    if any(kw in message for kw in ["本周趋势", "本周每天", "本周走势", "本周消费趋势"]):
        start_this_week = now - timedelta(days=now.weekday())
        end_this_week = now
        conn_q = get_db()
        reply = build_week_trend_reply(conn_q, user_id, start_this_week, end_this_week, "本周")
        conn_q.close()
        return jsonify({"success": True, "reply": reply, "type": "query"})

    date_range = extract_date_range_query(message, now)
    if date_range and (is_query_intent(message) or "到" in message or "至" in message):
        start_date, end_date, label = date_range
        conn_q = get_db()
        reply = build_period_summary_reply(conn_q, user_id, start_date, end_date, label)
        conn_q.close()
        return jsonify({"success": True, "reply": reply, "type": "query"})

    ym_query = extract_year_month_query(message, now)
    if ym_query and (is_query_intent(message) or "月" in message):
        start_date, end_date, label = ym_query
        conn_q = get_db()
        reply = build_period_summary_reply(conn_q, user_id, start_date, end_date, label)
        conn_q.close()
        return jsonify({"success": True, "reply": reply, "type": "query"})

    keyword_match = re.search(r"(?:查|搜索|统计|看看)\s*([\w\u4e00-\u9fff]{1,20})", message)
    if keyword_match and ("关键词" in message or not any(k in message for k in ["本月", "上月", "上周", "今天", "昨天"])):
        keyword = keyword_match.group(1).strip()
        if keyword and keyword not in {"账单", "消费", "支出", "收入", "趋势", "图表"}:
            start_date, end_date, label = (datetime(now.year, now.month, 1), now, "本月")
            ym = extract_year_month_query(message, now)
            if ym:
                start_date, end_date, label = ym
            conn_q = get_db()
            reply = build_keyword_summary_reply(conn_q, user_id, keyword, start_date, end_date, label)
            conn_q.close()
            return jsonify({"success": True, "reply": reply, "type": "query"})

    multi_entries = split_bill_entries(message)
    if len(multi_entries) >= 2:
        parsed_entries = []
        for entry in multi_entries:
            parsed = parse_bill(entry)
            if parsed is not None:
                parsed_entries.append((entry, parsed))

        if len(parsed_entries) >= 2:
            conn_multi = get_db()
            inserted = []
            for original_text, parsed in parsed_entries:
                amount, category, description, bill_type = parsed
                cursor = conn_multi.execute(
                    "INSERT INTO bills (user_id, amount, category, description, bill_type, created_at, year, month, day) VALUES (?,?,?,?,?,?,?,?,?)",
                    (user_id, amount, category, description, bill_type, now.isoformat(), now.year, now.month, now.day)
                )
                inserted.append({
                    "id": cursor.lastrowid,
                    "amount": amount,
                    "category": category,
                    "description": description,
                    "bill_type": bill_type,
                    "source": original_text,
                })
            conn_multi.commit()
            conn_multi.close()

            lines = [f"🧾 已拆分记录 {len(inserted)} 笔："]
            for item in inserted:
                prefix = "收入" if item["bill_type"] == "income" else "支出"
                lines.append(f"- {item['category']} {prefix} ¥{item['amount']:.2f}（{item['description']}）")
            lines.append("如有误，可发送“撤销上一笔”。")
            return jsonify({
                "success": True,
                "reply": "\n".join(lines),
                "type": "add-multi",
                "bills": inserted,
            })

    result = parse_bill(message)
    if result is None:
        # === 今天 ===
        if any(kw in message for kw in ["今天", "今日", "今天花了"]):
            now = datetime.now()
            conn_q = get_db()
            rows = conn_q.execute(
                "SELECT * FROM bills WHERE user_id=? AND year=? AND month=? AND day=? ORDER BY created_at DESC",
                (user_id, now.year, now.month, now.day),
            ).fetchall()
            conn_q.close()
            if not rows:
                reply = "今天还没有记账哦 😊 快来记一笔！"
            else:
                total_exp = sum(r["amount"] for r in rows if r["bill_type"] == "expense")
                total_inc = sum(r["amount"] for r in rows if r["bill_type"] == "income")
                lines = [f"📅 今天（{now.month}/{now.day}）共 {len(rows)} 笔："]
                for r in rows:
                    prefix = "+" if r["bill_type"] == "income" else "-"
                    lines.append(f"  {r['category']} {prefix}¥{r['amount']:.2f} {r['description']}")
                lines.append(f"💸 今日支出：¥{total_exp:.2f}")
                if total_inc > 0:
                    lines.append(f"💰 今日收入：¥{total_inc:.2f}")
                reply = "\n".join(lines)
            return jsonify({"success": True, "reply": reply, "type": "query"})

        # === 昨天 ===
        if any(kw in message for kw in ["昨天", "昨日"]):
            now = datetime.now()
            yesterday = now - timedelta(days=1)
            conn_q = get_db()
            rows = conn_q.execute(
                "SELECT * FROM bills WHERE user_id=? AND year=? AND month=? AND day=? ORDER BY created_at DESC",
                (user_id, yesterday.year, yesterday.month, yesterday.day),
            ).fetchall()
            conn_q.close()
            if not rows:
                reply = "昨天没有找到记账记录。"
            else:
                total_exp = sum(r["amount"] for r in rows if r["bill_type"] == "expense")
                lines = [f"📅 昨天（{yesterday.month}/{yesterday.day}）共 {len(rows)} 笔："]
                for r in rows:
                    prefix = "+" if r["bill_type"] == "income" else "-"
                    lines.append(f"  {r['category']} {prefix}¥{r['amount']:.2f} {r['description']}")
                lines.append(f"💸 昨日支出：¥{total_exp:.2f}")
                reply = "\n".join(lines)
            return jsonify({"success": True, "reply": reply, "type": "query"})

        # === 上月 ===
        if any(kw in message for kw in ["上月", "上个月"]):
            now = datetime.now()
            last = now.replace(day=1) - timedelta(days=1)
            summary = get_month_summary(last.year, last.month, user_id)
            reply = format_summary_reply(summary, last.year, last.month)
            return jsonify({"success": True, "reply": reply, "type": "query"})

        # === 分类查询（如"查餐饮"/"餐饮花了多少"） ===
        cat_names = [c for c in CATEGORY_KEYWORDS if c != "其他"]
        matched_cat = next((c for c in cat_names if c in message), None)
        if matched_cat and (any(kw in message for kw in ["查", "多少", "花了", "花费", "支出"]) or len(message) <= 4):
            now = datetime.now()
            conn_q = get_db()
            row = conn_q.execute(
                "SELECT SUM(amount) as total, COUNT(*) as cnt FROM bills "
                "WHERE user_id=? AND year=? AND month=? AND category=? AND bill_type='expense'",
                (user_id, now.year, now.month, matched_cat),
            ).fetchone()
            conn_q.close()
            total = row["total"] or 0
            cnt = row["cnt"] or 0
            reply = f"📊 本月{matched_cat}支出：¥{total:.2f}（共 {cnt} 笔）"
            return jsonify({"success": True, "reply": reply, "type": "query"})

        # === 本月汇总 ===
        if any(kw in message for kw in ["本月", "这月", "今月", "账单", "统计", "总共", "花了多少"]):
            now = datetime.now()
            summary = get_month_summary(now.year, now.month, user_id)
            reply = format_summary_reply(summary, now.year, now.month)
            return jsonify({"success": True, "reply": reply, "type": "query"})

        return jsonify({
            "success": False,
            "reply": (
                "没有识别到金额，请重新输入，例如：吃饭10块、打车20元\n\n"
                "你也可以这样问：\n"
                "- 今天 / 本月 / 上周 / 3月\n"
                "- 3月1日到3月15日消费\n"
                "- 查滴滴 / 统计星巴克\n"
                "- 本周趋势 / 可视化\n"
                "- 撤销上一笔 / 恢复上一笔\n"
                "- 发送 帮助 查看完整说明"
            ),
        })

    amount, category, description, bill_type = result
    now = datetime.now()

    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO bills (user_id, amount, category, description, bill_type, created_at, year, month, day) VALUES (?,?,?,?,?,?,?,?,?)",
        (user_id, amount, category, description, bill_type, now.isoformat(), now.year, now.month, now.day)
    )
    bill_id = cursor.lastrowid
    conn.commit()
    conn.close()

    emoji_map = {
        "餐饮": "🍜", "交通": "🚕", "购物": "🛍️", "娱乐": "🎮",
        "居家": "🏠", "医疗": "💊", "教育": "📚", "人情": "🧧",
        "收入": "💰", "其他": "📝"
    }
    emoji = emoji_map.get(category, "📝")
    type_text = "收入" if bill_type == "income" else "支出"

    reply = f"{emoji} 已记录{type_text}！\n📌 {description}\n💵 金额：¥{amount:.2f}\n🏷️ 分类：{category}"
    return jsonify({
        "success": True,
        "reply": reply,
        "type": "add",
        "bill": {
            "id": bill_id,
            "amount": amount,
            "category": category,
            "description": description,
            "bill_type": bill_type,
            "created_at": now.isoformat()
        }
    })

@app.route("/api/bills", methods=["GET"])
def get_bills():
    """获取账单列表"""
    auth_error = require_user_auth()
    if auth_error:
        return auth_error

    year = request.args.get("year", datetime.now().year, type=int)
    month = request.args.get("month", datetime.now().month, type=int)
    bill_type = request.args.get("type", "all")
    user_id = get_request_user_id()

    conn = get_db()
    if bill_type == "all":
        rows = conn.execute(
            "SELECT * FROM bills WHERE user_id=? AND year=? AND month=? ORDER BY created_at DESC",
            (user_id, year, month)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM bills WHERE user_id=? AND year=? AND month=? AND bill_type=? ORDER BY created_at DESC",
            (user_id, year, month, bill_type)
        ).fetchall()
    conn.close()

    bills = [dict(row) for row in rows]
    return jsonify({"success": True, "bills": bills})

@app.route("/api/summary", methods=["GET"])
def get_summary():
    """获取月度汇总"""
    auth_error = require_user_auth()
    if auth_error:
        return auth_error

    year = request.args.get("year", datetime.now().year, type=int)
    month = request.args.get("month", datetime.now().month, type=int)
    summary = get_month_summary(year, month, get_request_user_id())
    return jsonify({"success": True, "summary": summary})

@app.route("/api/budget", methods=["GET", "POST"])
def budget():
    """设置/获取预算"""
    auth_error = require_user_auth()
    if auth_error:
        return auth_error

    year = request.args.get("year", datetime.now().year, type=int)
    month = request.args.get("month", datetime.now().month, type=int)
    user_id = get_request_user_id()

    if request.method == "POST":
        data = request.get_json()
        amount = data.get("amount", 0)
        conn = get_db()
        existing = conn.execute(
            "SELECT id FROM budgets WHERE user_id=? AND year=? AND month=?",
            (user_id, year, month)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE budgets SET amount=? WHERE id=?",
                (amount, existing["id"])
            )
        else:
            conn.execute(
                "INSERT INTO budgets (user_id, year, month, amount) VALUES (?,?,?,?)",
                (user_id, year, month, amount)
            )
        conn.commit()
        conn.close()
        return jsonify({"success": True, "message": "预算设置成功"})
    else:
        conn = get_db()
        row = conn.execute(
            "SELECT amount FROM budgets WHERE user_id=? AND year=? AND month=?", (user_id, year, month)
        ).fetchone()
        conn.close()
        return jsonify({"success": True, "budget": row["amount"] if row else 0})

@app.route("/api/bills/<int:bill_id>", methods=["DELETE"])
def delete_bill(bill_id):
    """删除账单"""
    auth_error = require_user_auth()
    if auth_error:
        return auth_error

    user_id = get_request_user_id()
    conn = get_db()
    conn.execute("DELETE FROM bills WHERE id=? AND user_id=?", (bill_id, user_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "删除成功"})


@app.route("/api/bills/latest", methods=["DELETE"])
def delete_latest_bill():
    """删除当前用户最新一条账单。"""
    auth_error = require_user_auth()
    if auth_error:
        return auth_error

    user_id = get_request_user_id()
    conn = get_db()
    latest = conn.execute(
        "SELECT id FROM bills WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if not latest:
        conn.close()
        return jsonify({"success": False, "message": "暂无可删除账单"}), 404

    conn.execute("DELETE FROM bills WHERE id=? AND user_id=?", (latest["id"], user_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "已删除最新一条账单", "id": latest["id"]})

@app.route("/api/bills/<int:bill_id>", methods=["PUT"])
def update_bill(bill_id):
    """更新账单"""
    auth_error = require_user_auth()
    if auth_error:
        return auth_error

    data = request.get_json()
    user_id = data.get("user_id") or get_request_user_id()
    conn = get_db()
    conn.execute(
        "UPDATE bills SET amount=?, category=?, description=?, bill_type=? WHERE id=? AND user_id=?",
        (data["amount"], data["category"], data["description"], data["bill_type"], bill_id, user_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "更新成功"})

@app.route("/api/trend", methods=["GET"])
def get_trend():
    """获取近6个月趋势"""
    auth_error = require_user_auth()
    if auth_error:
        return auth_error

    user_id = get_request_user_id()
    conn = get_db()
    rows = conn.execute("""
        SELECT year, month,
            SUM(CASE WHEN bill_type='expense' THEN amount ELSE 0 END) as expense,
            SUM(CASE WHEN bill_type='income' THEN amount ELSE 0 END) as income
        FROM bills
        WHERE user_id=?
        GROUP BY year, month
        ORDER BY year DESC, month DESC
        LIMIT 6
    """, (user_id,)).fetchall()
    conn.close()
    trend = [dict(row) for row in reversed(rows)]
    return jsonify({"success": True, "trend": trend})

@app.route("/api/bills/add", methods=["POST"])
def add_bill_manual():
    """手动添加账单"""
    auth_error = require_user_auth()
    if auth_error:
        return auth_error

    data = request.get_json()
    now = datetime.now()
    user_id = data.get("user_id") or DEFAULT_USER_ID
    # 支持自定义日期
    bill_date_str = data.get("date", now.strftime("%Y-%m-%d"))
    try:
        bill_date = datetime.strptime(bill_date_str, "%Y-%m-%d")
    except Exception:
        bill_date = now

    conn = get_db()
    conn.execute(
        "INSERT INTO bills (user_id, amount, category, description, bill_type, created_at, year, month, day) VALUES (?,?,?,?,?,?,?,?,?)",
        (user_id, data["amount"], data["category"], data["description"],
         data.get("bill_type", "expense"),
         bill_date.isoformat(), bill_date.year, bill_date.month, bill_date.day)
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "添加成功"})


@app.route("/api/wechat-users", methods=["GET"])
def get_wechat_users():
    """获取微信用户申请/审批列表"""
    auth_error = require_admin_auth()
    if auth_error:
        return auth_error

    status = request.args.get("status", "all")
    conn = get_db()

    query = """
        SELECT wu.user_id, wu.status, wu.display_name, wu.apply_nickname,
               wu.apply_contact_tail, wu.requested_note,
               wu.requested_at, wu.approved_at, wu.approved_by,
               COUNT(b.id) AS bill_count
        FROM wechat_users wu
        LEFT JOIN bills b ON b.user_id = wu.user_id
    """
    params = []
    if status != "all":
        query += " WHERE wu.status=?"
        params.append(status)
    query += " GROUP BY wu.user_id ORDER BY CASE wu.status WHEN 'pending' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, wu.requested_at DESC, wu.approved_at DESC"

    rows = conn.execute(query, params).fetchall()
    counts = conn.execute(
        """
        SELECT status, COUNT(*) AS total
        FROM wechat_users
        GROUP BY status
        """
    ).fetchall()
    conn.close()

    count_map = {row["status"]: row["total"] for row in counts}
    return jsonify({
        "success": True,
        "users": [dict(row) for row in rows],
        "counts": {
            "pending": count_map.get("pending", 0),
            "approved": count_map.get("approved", 0),
            "admin": count_map.get("admin", 0),
            "rejected": count_map.get("rejected", 0),
        },
    })


@app.route("/api/wechat-users/<path:user_id>/approve", methods=["POST"])
def approve_wechat_user(user_id):
    """批准微信用户使用权限"""
    auth_error = require_admin_auth()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}
    approver = data.get("approved_by") or DEFAULT_USER_ID
    conn = get_db()
    updated = conn.execute(
        "UPDATE wechat_users SET status='approved', approved_at=CURRENT_TIMESTAMP, approved_by=? WHERE user_id=?",
        (approver, user_id),
    )
    if updated.rowcount > 0:
        write_approval_log(conn, user_id, "approve", approver, "web", "网页审批通过")
    conn.commit()
    conn.close()
    if updated.rowcount == 0:
        return jsonify({"success": False, "message": "未找到该用户申请"}), 404
    return jsonify({"success": True, "message": "用户已批准"})


@app.route("/api/wechat-users/<path:user_id>/reject", methods=["POST"])
def reject_wechat_user(user_id):
    """拒绝微信用户使用权限"""
    auth_error = require_admin_auth()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}
    approver = data.get("approved_by") or DEFAULT_USER_ID
    conn = get_db()
    updated = conn.execute(
        "UPDATE wechat_users SET status='rejected', approved_at=CURRENT_TIMESTAMP, approved_by=? WHERE user_id=?",
        (approver, user_id),
    )
    if updated.rowcount > 0:
        write_approval_log(conn, user_id, "reject", approver, "web", "网页审批拒绝")
    conn.commit()
    conn.close()
    if updated.rowcount == 0:
        return jsonify({"success": False, "message": "未找到该用户申请"}), 404
    return jsonify({"success": True, "message": "用户已拒绝"})


@app.route("/api/wechat-users/<path:user_id>", methods=["PUT"])
def update_wechat_user(user_id):
    """更新微信用户备注名等信息"""
    auth_error = require_admin_auth()
    if auth_error:
        return auth_error

    data = request.get_json() or {}
    display_name = (data.get("display_name") or "").strip()
    apply_nickname = (data.get("apply_nickname") or "").strip()
    apply_contact_tail = (data.get("apply_contact_tail") or "").strip()
    requested_note = (data.get("requested_note") or "").strip()

    conn = get_db()
    existing = conn.execute(
        "SELECT user_id FROM wechat_users WHERE user_id=?",
        (user_id,),
    ).fetchone()
    if not existing:
        conn.close()
        return jsonify({"success": False, "message": "未找到该用户"}), 404

    conn.execute(
        "UPDATE wechat_users SET display_name=?, apply_nickname=?, apply_contact_tail=?, requested_note=? WHERE user_id=?",
        (display_name, apply_nickname, apply_contact_tail, requested_note, user_id),
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "用户信息已更新"})


@app.route("/api/admin/auth-check", methods=["GET"])
def admin_auth_check():
    """用于网页管理端校验管理员口令。"""
    auth_error = require_admin_auth()
    if auth_error:
        return auth_error
    return jsonify({"success": True, "message": "鉴权通过"})


@app.route("/api/approval-logs", methods=["GET"])
def get_approval_logs():
    """获取审批日志（管理员接口）。"""
    auth_error = require_admin_auth()
    if auth_error:
        return auth_error

    limit = request.args.get("limit", 50, type=int)
    limit = max(1, min(limit, 200))

    conn = get_db()
    rows = conn.execute(
        """
        SELECT id, user_id, action, operator, channel, note, created_at
        FROM approval_logs
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return jsonify({"success": True, "logs": [dict(row) for row in rows]})


@app.route("/api/bills/export", methods=["GET"])
def export_bills():
    """导出当月账单为 CSV（BOM 格式，Excel 直接打开不乱码）"""
    auth_error = require_user_auth()
    if auth_error:
        return auth_error

    year = request.args.get("year", datetime.now().year, type=int)
    month = request.args.get("month", datetime.now().month, type=int)
    user_id = get_request_user_id()

    conn = get_db()
    rows = conn.execute(
        "SELECT amount, category, description, bill_type, year, month, day "
        "FROM bills WHERE user_id=? AND year=? AND month=? ORDER BY year, month, day, id",
        (user_id, year, month),
    ).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["日期", "金额", "类型", "分类", "描述"])
    for row in rows:
        bill_type = "收入" if row["bill_type"] == "income" else "支出"
        date_str = f"{row['year']}-{row['month']:02d}-{row['day']:02d}"
        writer.writerow([date_str, row["amount"], bill_type, row["category"], row["description"]])

    from flask import Response
    return Response(
        "\ufeff" + output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=bills_{year}_{month:02d}.csv"},
    )


# ==================== 辅助函数 ====================

def get_month_summary(year, month, user_id=DEFAULT_USER_ID):
    conn = get_db()
    expense = conn.execute(
        "SELECT SUM(amount) as total FROM bills WHERE user_id=? AND year=? AND month=? AND bill_type='expense'",
        (user_id, year, month)
    ).fetchone()["total"] or 0

    income = conn.execute(
        "SELECT SUM(amount) as total FROM bills WHERE user_id=? AND year=? AND month=? AND bill_type='income'",
        (user_id, year, month)
    ).fetchone()["total"] or 0

    categories = conn.execute(
        "SELECT category, SUM(amount) as total FROM bills WHERE user_id=? AND year=? AND month=? AND bill_type='expense' GROUP BY category ORDER BY total DESC",
        (user_id, year, month)
    ).fetchall()

    count = conn.execute(
        "SELECT COUNT(*) as cnt FROM bills WHERE user_id=? AND year=? AND month=?",
        (user_id, year, month)
    ).fetchone()["cnt"]

    conn.close()
    return {
        "expense": expense,
        "income": income,
        "balance": income - expense,
        "count": count,
        "categories": [dict(row) for row in categories]
    }

def format_summary_reply(summary, year, month):
    lines = [f"📊 {year}年{month}月账单统计"]
    lines.append(f"💸 总支出：¥{summary['expense']:.2f}")
    lines.append(f"💰 总收入：¥{summary['income']:.2f}")
    lines.append(f"💎 结余：¥{summary['balance']:.2f}")
    lines.append(f"📝 记录笔数：{summary['count']}笔")
    if summary["categories"]:
        lines.append("\n📌 各类支出：")
        for cat in summary["categories"][:5]:
            lines.append(f"  {cat['category']}：¥{cat['total']:.2f}")
    return "\n".join(lines)

# ==================== 启动 ====================

if __name__ == "__main__":
    init_db()
    if register_wechat_routes is not None:
        register_wechat_routes(
            app,
            init_db,
            parse_bill,
            get_db,
            get_month_summary,
            format_summary_reply,
        )
        print("✅ 微信路由已启用：/wechat")
    else:
        print("ℹ️ 未启用微信路由（可安装 wechatpy 后自动启用）")
    print(f"✅ 记账小助手启动成功！访问 http://localhost:{APP_PORT}")
    app.run(host=APP_HOST, port=APP_PORT, debug=APP_DEBUG)
