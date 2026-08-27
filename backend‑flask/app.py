from flask import Flask, request, jsonify, render_template_string
import sqlite3
import json
import time
from datetime import datetime, timedelta

app = Flask(__name__)
DATABASE = 'orders.db'

# --------------------- 数据库工具函数 ---------------------

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """初始化数据库，创建订单表（如果不存在）"""
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_no TEXT UNIQUE NOT NULL,
                items TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                estimated_time INTEGER DEFAULT 0,
                deadline TIMESTAMP                -- 新增：预计完成时间
            )
        ''')
        conn.commit()

# --------------------- 业务逻辑辅助函数 ---------------------

def calculate_wait_time():
    """计算当前队列的平均等待时间（简单示例：每个未完成订单平均5分钟）"""
    with get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status IN ('pending', 'cooking')"
        ).fetchone()[0]
    return count * 300

def update_all_estimated_times():
    """更新所有未完成订单的预计等待时间和预计完成时间"""
    wait = calculate_wait_time()
    # 计算预计完成时间：当前时间 + 等待时间
    deadline = datetime.now() + timedelta(seconds=wait)
    deadline_str = deadline.strftime('%Y-%m-%d %H:%M:%S')
    with get_db() as conn:
        conn.execute(
            "UPDATE orders SET estimated_time = ?, deadline = ? WHERE status IN ('pending', 'cooking')",
            (wait, deadline_str)
        )
        conn.commit()

def get_urgent_orders(threshold_seconds=300):
    """
    获取即将超时的订单
    threshold_seconds: 剩余时间小于等于该值时视为紧急，默认5分钟
    返回：订单列表，每个订单包含 id, order_no, items, status, created_at, deadline, remaining_seconds
    """
    urgent = []
    now = datetime.now()
    with get_db() as conn:
        # 只查询未完成的订单
        rows = conn.execute(
            "SELECT * FROM orders WHERE status IN ('pending', 'cooking') AND deadline IS NOT NULL"
        ).fetchall()

        for row in rows:
            # 解析 deadline 字符串为 datetime 对象
            deadline_str = row['deadline']
            if not deadline_str:
                continue
            deadline = datetime.strptime(deadline_str, '%Y-%m-%d %H:%M:%S')
            remaining = (deadline - now).total_seconds()
            if remaining <= threshold_seconds:
                order = {
                    'id': row['id'],
                    'order_no': row['order_no'],
                    'items': json.loads(row['items']),
                    'status': row['status'],
                    'created_at': row['created_at'],
                    'deadline': deadline_str,
                    'remaining_seconds': int(remaining)
                }
                urgent.append(order)
    # 按剩余时间升序排列（最紧急的在前）
    urgent.sort(key=lambda x: x['remaining_seconds'])
    return urgent

# --------------------- API 路由 ---------------------

@app.route('/api/order', methods=['POST'])
def add_order():
    """
    添加新订单
    请求体 JSON:
    {
        "order_no": "A001",
        "items": ["宫保鸡丁", "米饭"]
    }
    """
    try:
        data = request.get_json()
        if not data or 'order_no' not in data or 'items' not in data:
            return jsonify({'success': False, 'message': '缺少必要字段'}), 400

        order_no = data['order_no']
        items = json.dumps(data['items'], ensure_ascii=False)

        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM orders WHERE order_no = ?", (order_no,)
            ).fetchone()
            if existing:
                return jsonify({'success': False, 'message': '订单号已存在'}), 409

            # 插入订单时不设置 deadline，等 update_all_estimated_times 统一设置
            cursor = conn.execute(
                "INSERT INTO orders (order_no, items) VALUES (?, ?)",
                (order_no, items)
            )
            conn.commit()
            new_id = cursor.lastrowid

        # 更新所有未完成订单的预计时间和 deadline
        update_all_estimated_times()

        return jsonify({
            'success': True,
            'order_id': new_id,
            'message': '订单已添加'
        }), 201

    except Exception as e:
        app.logger.error(f"添加订单失败: {e}")
        return jsonify({'success': False, 'message': '服务器内部错误'}), 500

@app.route('/api/orders', methods=['GET'])
def get_orders():
    """获取所有订单（或按状态筛选）"""
    try:
        status_filter = request.args.get('status')
        query = "SELECT * FROM orders"
        params = ()
        if status_filter:
            query += " WHERE status = ?"
            params = (status_filter,)

        with get_db() as conn:
            rows = conn.execute(query, params).fetchall()

        orders = []
        for row in rows:
            orders.append({
                'id': row['id'],
                'order_no': row['order_no'],
                'items': json.loads(row['items']),
                'status': row['status'],
                'created_at': row['created_at'],
                'estimated_time': row['estimated_time'],
                'deadline': row['deadline']
            })
        return jsonify({'success': True, 'orders': orders})
    except Exception as e:
        app.logger.error(f"获取订单失败: {e}")
        return jsonify({'success': False, 'message': '服务器内部错误'}), 500

@app.route('/api/order/<int:order_id>/status', methods=['PUT'])
def update_order_status(order_id):
    """更新订单状态"""
    try:
        data = request.get_json()
        new_status = data.get('status') if data else None
        if new_status not in ('pending', 'cooking', 'done', 'cancelled'):
            return jsonify({'success': False, 'message': '无效的状态'}), 400

        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if not existing:
                return jsonify({'success': False, 'message': '订单不存在'}), 404

            conn.execute(
                "UPDATE orders SET status = ? WHERE id = ?",
                (new_status, order_id)
            )
            conn.commit()

        # 如果状态变为 done 或 cancelled，需要重新计算队列等待时间和 deadline
        if new_status in ('done', 'cancelled'):
            update_all_estimated_times()
        # 如果状态变为 cooking 或 pending，deadline 不变（简单处理）

        return jsonify({'success': True, 'message': '状态已更新'})
    except Exception as e:
        app.logger.error(f"更新订单状态失败: {e}")
        return jsonify({'success': False, 'message': '服务器内部错误'}), 500

@app.route('/api/queue-info', methods=['GET'])
def get_queue_info():
    """获取当前队列摘要信息"""
    try:
        with get_db() as conn:
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE status = 'pending'"
            ).fetchone()[0]
            cooking_count = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE status = 'cooking'"
            ).fetchone()[0]
        total_wait = calculate_wait_time()
        urgent_orders = get_urgent_orders()
        return jsonify({
            'success': True,
            'pending_count': pending_count,
            'cooking_count': cooking_count,
            'avg_wait_time': total_wait,
            'estimated_total_wait': total_wait,
            'urgent_count': len(urgent_orders)   # 紧急订单数量
        })
    except Exception as e:
        app.logger.error(f"获取队列信息失败: {e}")
        return jsonify({'success': False, 'message': '服务器内部错误'}), 500

# 新增：紧急订单 API
@app.route('/api/urgent-orders', methods=['GET'])
def urgent_orders():
    """获取即将超时的订单列表"""
    try:
        # 可以自定义阈值，通过查询参数 ?threshold=300 （单位秒）
        threshold = request.args.get('threshold', default=300, type=int)
        urgent = get_urgent_orders(threshold)
        return jsonify({'success': True, 'urgent_orders': urgent})
    except Exception as e:
        app.logger.error(f"获取紧急订单失败: {e}")
        return jsonify({'success': False, 'message': '服务器内部错误'}), 500

# 简单前端页面
@app.route('/')
def index():
    # 使用内嵌 HTML，自动刷新紧急订单
    html = '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>备餐预警 - 即将超时订单</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; }
            table { border-collapse: collapse; width: 80%; }
            th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
            th { background-color: #f2f2f2; }
            .urgent { color: red; font-weight: bold; }
            .no-urgent { color: green; }
        </style>
    </head>
    <body>
        <h1>即将超时订单</h1>
        <p id="last-update">更新于：--</p>
        <table id="urgent-table">
            <thead>
                <tr>
                    <th>订单号</th>
                    <th>菜品</th>
                    <th>状态</th>
                    <th>剩余时间</th>
                </tr>
            </thead>
            <tbody>
                <!-- 动态填充 -->
            </tbody>
        </table>
        <p id="no-urgent" class="no-urgent" style="display:none;">当前没有即将超时的订单</p>

        <script>
            function fetchUrgentOrders() {
                fetch('/api/urgent-orders')
                    .then(response => response.json())
                    .then(data => {
                        const tbody = document.querySelector('#urgent-table tbody');
                        const noUrgent = document.getElementById('no-urgent');
                        tbody.innerHTML = '';
                        if (data.success && data.urgent_orders.length > 0) {
                            noUrgent.style.display = 'none';
                            data.urgent_orders.forEach(order => {
                                const row = tbody.insertRow();
                                row.innerHTML = `
                                    <td>${order.order_no}</td>
                                    <td>${order.items.join(', ')}</td>
                                    <td>${order.status}</td>
                                    <td class="urgent">${formatRemaining(order.remaining_seconds)}</td>
                                `;
                            });
                        } else {
                            noUrgent.style.display = 'block';
                        }
                        document.getElementById('last-update').textContent = '更新于：' + new Date().toLocaleTimeString();
                    })
                    .catch(err => {
                        console.error('获取数据失败:', err);
                    });
            }

            function formatRemaining(seconds) {
                if (seconds <= 0) return '已超时！';
                const mins = Math.floor(seconds / 60);
                const secs = seconds % 60;
                return `${mins}分${secs}秒`;
            }

            // 首次加载
            fetchUrgentOrders();
            // 每5秒刷新
            setInterval(fetchUrgentOrders, 5000);
        </script>
    </body>
    </html>
    '''
    return render_template_string(html)

# --------------------- 启动应用 ---------------------

if __name__ == '__main__':
    init_db()  # 启动时初始化数据库
    app.run(host='0.0.0.0', port=5000, debug=True)
