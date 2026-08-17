from flask import Flask, request, jsonify, render_template, redirect
from flask_sqlalchemy import SQLAlchemy
import os
import time
import uuid

app = Flask(__name__)

# MySQL connection using environment variables
# In Docker Compose, the hostname is the service name (mysql_db)
MYSQL_HOST = os.environ.get('MYSQL_HOST', 'mysql_db')
MYSQL_PORT = os.environ.get('MYSQL_PORT', '3306')
MYSQL_USER = os.environ.get('MYSQL_USER', 'root')
MYSQL_PASSWORD = os.environ.get('MYSQL_PASSWORD', 'rootpassword')
MYSQL_DATABASE = os.environ.get('MYSQL_DATABASE', 'tododb')

app.config['SQLALCHEMY_DATABASE_URI'] = (
    f'mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)


# Todo model
class Todo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    list_id = db.Column(db.String(8), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    completed = db.Column(db.Boolean, default=False)
    position = db.Column(db.Integer, nullable=False, default=0)

    def to_dict(self):
        return {
            'id': self.id,
            'list_id': self.list_id,
            'title': self.title,
            'completed': self.completed,
            'position': self.position
        }


# Root route - generates a unique list and redirects
@app.route('/')
def index():
    """Generate a new unique list ID and redirect to it."""
    list_id = uuid.uuid4().hex[:8]
    return redirect(f'/list/{list_id}')


# Serve the UI for a specific list
@app.route('/list/<list_id>')
def show_list(list_id):
    """Serve the todo app UI for a specific list."""
    return render_template('index.html', list_id=list_id)


@app.route('/api/<list_id>/todos', methods=['GET'])
def get_todos(list_id):
    """Get all todos for a specific list ordered by position."""
    todos = Todo.query.filter_by(list_id=list_id).order_by(Todo.position).all()
    return jsonify([todo.to_dict() for todo in todos]), 200


@app.route('/api/<list_id>/todo', methods=['POST'])
def add_todo(list_id):
    """Add a new todo to a specific list."""
    data = request.get_json()

    if not data or 'title' not in data:
        return jsonify({'error': 'Title is required'}), 400

    # Get the next position (append to end) for this list
    max_pos = db.session.query(db.func.max(Todo.position)).filter(
        Todo.list_id == list_id
    ).scalar()
    next_pos = (max_pos + 1) if max_pos is not None else 0

    todo = Todo(title=data['title'], position=next_pos, list_id=list_id)
    db.session.add(todo)
    db.session.commit()

    return jsonify(todo.to_dict()), 201


@app.route('/api/<list_id>/todo/insert/index', methods=['POST'])
def insert_todo_at_index(list_id):
    """Insert a todo at a specific index position in a list."""
    data = request.get_json()

    if not data or 'title' not in data or 'index' not in data:
        return jsonify({'error': 'Title and index are required'}), 400

    index = data['index']
    title = data['title']

    total = Todo.query.filter_by(list_id=list_id).count()

    if index < 0 or index > total:
        return jsonify({'error': 'Index out of range'}), 400

    # Shift all todos at or after this index down by 1 (only in this list)
    Todo.query.filter(Todo.list_id == list_id, Todo.position >= index).update(
        {Todo.position: Todo.position + 1}
    )

    # Insert new todo at the desired position
    todo = Todo(title=title, position=index, list_id=list_id)
    db.session.add(todo)
    db.session.commit()

    return jsonify(todo.to_dict()), 201


@app.route('/api/<list_id>/todo/<int:todo_id>', methods=['PUT'])
def update_todo(list_id, todo_id):
    """Update a todo (toggle completed)."""
    todo = Todo.query.filter_by(id=todo_id, list_id=list_id).first()

    if not todo:
        return jsonify({'error': 'Todo not found'}), 404

    data = request.get_json()
    if 'completed' in data:
        todo.completed = data['completed']

    db.session.commit()
    return jsonify(todo.to_dict()), 200


@app.route('/api/<list_id>/todo/<int:todo_id>', methods=['DELETE'])
def delete_todo(list_id, todo_id):
    """Delete a todo."""
    todo = Todo.query.filter_by(id=todo_id, list_id=list_id).first()

    if not todo:
        return jsonify({'error': 'Todo not found'}), 404

    db.session.delete(todo)
    db.session.commit()
    return jsonify({'message': 'Todo deleted'}), 200


def wait_for_db(retries=10, delay=3):
    """Wait for MySQL to be ready before starting the app."""
    for attempt in range(retries):
        try:
            with app.app_context():
                db.create_all()
            print("Database connected successfully!")
            return True
        except Exception as e:
            print(f"Database not ready (attempt {attempt + 1}/{retries}): {e}")
            time.sleep(delay)
    print("Could not connect to database after all retries.")
    return False


if __name__ == '__main__':
    if wait_for_db():
        app.run(host='0.0.0.0', debug=True, port=5001)
    else:
        print("Exiting: Database unavailable.")
        exit(1)
