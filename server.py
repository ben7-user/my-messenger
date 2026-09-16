from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_socketio import SocketIO, emit, join_room
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import bcrypt
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'change-me-12345'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///messenger.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='eventlet')

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)

class Chat(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    is_group = db.Column(db.Boolean, default=False)
    name = db.Column(db.String(100), default=None)
    members = db.relationship('ChatMember', backref='chat', cascade='all, delete-orphan')

class ChatMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.Integer, db.ForeignKey('chat.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship('User')
    __table_args__ = (db.UniqueConstraint('chat_id', 'user_id'),)

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.Integer, db.ForeignKey('chat.id'), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    text = db.Column(db.Text, default='')
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    sender = db.relationship('User')

def hash_pwd(p):
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()

def check_pwd(p, h):
    return bcrypt.checkpw(p.encode(), h.encode())

def get_private_chat(a, b):
    sub = db.session.query(ChatMember.chat_id).filter(ChatMember.user_id.in_([a, b])).group_by(ChatMember.chat_id).having(db.func.count(ChatMember.user_id) == 2).subquery()
    c = Chat.query.filter(Chat.is_group == False, Chat.id.in_(db.session.query(sub))).first()
    if not c:
        c = Chat(is_group=False)
        db.session.add(c)
        db.session.flush()
        db.session.add(ChatMember(chat_id=c.id, user_id=a))
        db.session.add(ChatMember(chat_id=c.id, user_id=b))
        db.session.commit()
    return c

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('chat'))
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        u = request.form.get('username', '').strip()
        p = request.form.get('password', '')
        if not u or len(p) < 4:
            return render_template('index.html', error='Min 4 chars', mode='register')
        if User.query.filter_by(username=u).first():
            return render_template('index.html', error='User exists', mode='register')
        user = User(username=u, password_hash=hash_pwd(p))
        db.session.add(user)
        db.session.commit()
        session['user_id'] = user.id
        session['username'] = user.username
        return redirect(url_for('chat'))
    return render_template('index.html', mode='register')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = request.form.get('username', '').strip()
        p = request.form.get('password', '')
        user = User.query.filter_by(username=u).first()
        if user and check_pwd(p, user.password_hash):
            session['user_id'] = user.id
            session['username'] = user.username
            return redirect(url_for('chat'))
        return render_template('index.html', error='Wrong login', mode='login')
    return render_template('index.html', mode='login')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/chat')
def chat():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template('chat.html', username=session['username'], user_id=session['user_id'])

@app.route('/api/users')
def api_users():
    if 'user_id' not in session:
        return jsonify([])
    us = User.query.filter(User.id != session['user_id']).all()
    return jsonify([{'id': u.id, 'username': u.username} for u in us])

@app.route('/api/chats')
def api_chats():
    if 'user_id' not in session:
        return jsonify([])
    me = session['user_id']
    ids = [m.chat_id for m in ChatMember.query.filter_by(user_id=me).all()]
    result = []
    for cid in ids:
        c = Chat.query.get(cid)
        members = [{'id': m.user.id, 'username': m.user.username} for m in c.members]
        other = next((m for m in members if m['id'] != me), None)
        title = c.name if c.is_group else (other['username'] if other else 'Chat')
        last = Message.query.filter_by(chat_id=cid).order_by(Message.timestamp.desc()).first()
        result.append({'id': cid, 'is_group': c.is_group, 'title': title, 'members': members, 'last_message': {'text': last.text if last else ''} if last else None})
    return jsonify(result)

@app.route('/api/chats/private', methods=['POST'])
def api_private():
    if 'user_id' not in session:
        return jsonify({'error': 'no'}), 401
    other = (request.get_json() or {}).get('user_id')
    c = get_private_chat(session['user_id'], other)
    members = [{'id': m.user.id, 'username': m.user.username} for m in c.members]
    other_u = next((m for m in members if m['id'] != session['user_id']), None)
    return jsonify({'id': c.id, 'is_group': False, 'title': other_u['username'] if other_u else 'Chat', 'members': members})

@app.route('/api/chats/<int:cid>/messages')
def api_msgs(cid):
    if 'user_id' not in session:
        return jsonify([])
    ms = Message.query.filter_by(chat_id=cid).order_by(Message.timestamp.asc()).all()
    return jsonify([{'id': m.id, 'sender_id': m.sender_id, 'sender_name': m.sender.username, 'text': m.text, 'timestamp': m.timestamp.strftime('%H:%M')} for m in ms])

online = {}

@socketio.on('connect')
def on_conn():
    if 'user_id' not in session:
        return
    online[request.sid] = session['user_id']
    for m in ChatMember.query.filter_by(user_id=session['user_id']).all():
        join_room('chat_' + str(m.chat_id))

@socketio.on('disconnect')
def on_disc():
    online.pop(request.sid, None)

@socketio.on('send_message')
def on_msg(data):
    if 'user_id' not in session:
        return
    cid = data.get('chat_id')
    txt = (data.get('text') or '').strip()
    if not cid or not txt:
        return
    m = Message(chat_id=cid, sender_id=session['user_id'], text=txt)
    db.session.add(m)
    db.session.commit()
    payload = {'id': m.id, 'chat_id': cid, 'sender_id': m.sender_id, 'sender_name': session['username'], 'text': txt, 'timestamp': m.timestamp.strftime('%H:%M')}
    socketio.emit('new_message', payload, room='chat_' + str(cid))

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000)