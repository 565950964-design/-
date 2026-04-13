"""
记账小助手 - Flask 后端
支持自然语言解析记账、账单查询、统计分析
"""

import re
import os
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import sqlite3

try:
    from wechat_handler import register_wechat_routes
except Exception:
    register_wechat_routes = None

app = Flask(__name__, static_folder="../frontend", static_url_path="")
CORS(app)

DB_PATH = os.path.join(os.path.dirname(__file__), "../data/bills.db")
DEFAULT_USER_ID = "web-local"
ADMIN_WEB_TOKEN = os.getenv("ADMIN_WEB_TOKEN", "")
APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("PORT", os.getenv("APP_PORT", "5000")))
APP_DEBUG = os.getenv("FLASK_DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}

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

def parse_bill(text):
    """解析自然语言账单，返回 (amount, category, description, bill_type)"""
    text = text.strip()

    # 提取金额（支持多种格式：10块、10元、10.5、¥10）
    amount_patterns = [
        r'[¥￥]\s*(\d+(?:\.\d+)?)',
        r'(\d+(?:\.\d+)?)\s*(?:块钱|块|元钱|元|RMB)',
        r'(\d+(?:\.\d+)?)',
    ]
    amount = None
    for pattern in amount_patterns:
        match = re.search(pattern, text)
        if match:
            amount = float(match.group(1))
            break

    if amount is None:
        return None

    # 判断收入还是支出
    income_keywords = ["收入", "工资", "薪资", "奖金", "报销", "退款", "返现", "收到", "到账"]
    bill_type = "income" if any(kw in text for kw in income_keywords) else "expense"

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
    description = re.sub(r'[¥￥]\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*(?:块钱|块|元钱|元|RMB)', '', text).strip()
    if not description:
        description = text

    return amount, category, description, bill_type

# ==================== API 路由 ====================

@app.route("/")
def index():
    return send_from_directory("../frontend", "index.html")

@app.route("/api/chat", methods=["POST"])
def chat():
    """处理自然语言记账消息"""
    data = request.get_json()
    message = data.get("message", "").strip()
    user_id = data.get("user_id") or DEFAULT_USER_ID
    if not message:
        return jsonify({"success": False, "reply": "请输入记账内容"})

    result = parse_bill(message)
    if result is None:
        # 尝试回复查询类消息
        if any(kw in message for kw in ["本月", "这月", "今月", "账单", "统计", "总共", "花了多少"]):
            now = datetime.now()
            summary = get_month_summary(now.year, now.month, user_id)
            reply = format_summary_reply(summary, now.year, now.month)
            return jsonify({"success": True, "reply": reply, "type": "query"})
        return jsonify({"success": False, "reply": "没有识别到金额，请重新输入，例如：吃饭10块、打车20元"})

    amount, category, description, bill_type = result
    now = datetime.now()

    conn = get_db()
    conn.execute(
        "INSERT INTO bills (user_id, amount, category, description, bill_type, created_at, year, month, day) VALUES (?,?,?,?,?,?,?,?,?)",
        (user_id, amount, category, description, bill_type, now.isoformat(), now.year, now.month, now.day)
    )
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
    year = request.args.get("year", datetime.now().year, type=int)
    month = request.args.get("month", datetime.now().month, type=int)
    summary = get_month_summary(year, month, get_request_user_id())
    return jsonify({"success": True, "summary": summary})

@app.route("/api/budget", methods=["GET", "POST"])
def budget():
    """设置/获取预算"""
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
    user_id = get_request_user_id()
    conn = get_db()
    conn.execute("DELETE FROM bills WHERE id=? AND user_id=?", (bill_id, user_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "删除成功"})

@app.route("/api/bills/<int:bill_id>", methods=["PUT"])
def update_bill(bill_id):
    """更新账单"""
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
