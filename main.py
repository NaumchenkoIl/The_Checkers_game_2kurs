from typing import List, Tuple, Optional
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from AuthManager import UserManager, token_checker
import secrets
import socketio

app = FastAPI()

# Настройка CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Разрешить все источники (для тестирования; в продакшене укажите конкретные домены)
    allow_credentials=True,
    allow_methods=["*"],  # Разрешить все методы, включая OPTIONS
    allow_headers=["*"],  # Разрешить все заголовки
)

# Настройка socket.io сервера
sio = socketio.AsyncServer(
    async_mode='asgi',
    cors_allowed_origins='*',
    logger=True,
    engineio_logger=True,
    ping_timeout=20,
    ping_interval=5,
)
app.mount("/socket.io", socketio.ASGIApp(sio, socketio_path="/socket.io/")) 

user_manager = UserManager()
games = {} 
connections = {} 

BOARD_SIZE = 8  
EMPTY = 0  # как обозначаются клетки
WHITE = 1  
BLACK = 2   
WHITE_KING = 3   
BLACK_KING = 4   

class Move(BaseModel):
    sequence: List[Tuple[int, int]]  # последовательность координат хода

class UserRegister(BaseModel):
    username: str  # имя 
    email: str  # email 
    password: str  # пароль

class AuthData(BaseModel):
    username_or_email: str  # имя или email
    password: str  # пароль

class PasswordResetRequest(BaseModel):
    email: str  # email для запроса сброса пароля

class PasswordReset(BaseModel):
    reset_token: str  
    new_password: str  

def format_response(status: int = 0, error: Optional[str] = None, data: Optional[dict] = None):
    return {
        "status": status,  # 0 — успех, 1 — ошибка
        "error": error,  # ошибка, если есть
        "data": data or {}  # данные об игре
    }

class CheckersGame:
    def __init__(self):
        self.board = self.init_board()  # инициализируем игровую доску
        self.current_turn = WHITE  # белые начинают первыми (в процессе изменить на выбор: белые, черные, рандом)
        self.must_continue = False  # нужно ли продолжать рубить
        self.last_moved_piece = None  # последняя двигающаяся шашка
        self.white_name = None  # имя игрока за белых
        self.black_name = None  # имя игрока за чёрных
        self.eaten_white_pieces = []  # список съеденных белых шашек
        self.eaten_black_pieces = []  # список съеденных чёрных шашек
        self.last_move_by = None  # Кто сделал последний ход
        self.game_ended = False

    def init_board(self):  # стартовое положение шашек
        board = [[EMPTY] * BOARD_SIZE for _ in range(BOARD_SIZE)]
        for row in range(3):  # чёрные вверху
            for col in range(BOARD_SIZE):
                if (row + col) % 2 == 1:
                    board[row][col] = BLACK
        for row in range(5, 8):  # белые снизу
            for col in range(BOARD_SIZE):
                if (row + col) % 2 == 1:
                    board[row][col] = WHITE
        return board  # возвращаем инициализированную доску

    def get_board(self, player_color: str = None):  # текущее состояние доски с учётом перспективы
        if player_color == 'black':
            return [row[::-1] for row in self.board[::-1]]  # переворачивает доску для чёрных игроков
        return self.board

    def get_eaten_pieces(self):  # списки съеденных шашек
        return {
            "eaten_white_pieces": self.eaten_white_pieces,
            "eaten_black_pieces": self.eaten_black_pieces
        }

    def promote_to_king(self, x: int, y: int):  # шашку в дамку
        if self.board[y][x] == WHITE and y == 0:
            self.board[y][x] = WHITE_KING
        elif self.board[y][x] == BLACK and y == BOARD_SIZE - 1:
            self.board[y][x] = BLACK_KING

    def is_valid_move(self, move: Move, player: str) -> bool:  # проверка допустимости хода
        if self.game_ended:
            print("Game has ended, no moves allowed")
            return False

        sequence = move.sequence
        captured = False

        print(f"Validating move for player {player}: sequence={sequence}")
        if (self.current_turn == WHITE and player != self.white_name) or (
            self.current_turn == BLACK and player != self.black_name
        ):
            print(
                f"Invalid turn: current_turn={self.current_turn}, player={player}, white_name={self.white_name}, black_name={self.black_name}"
            )
            return False

        if self.must_continue and sequence[0] != self.last_moved_piece:  # проверка, что ход начинается с последней шашки, если требуется продолжение взятия
            print(f"Must continue with piece {self.last_moved_piece}, but got {sequence[0]}")
            return False

        for i in range(len(sequence) - 1):
            fx, fy = sequence[i]
            tx, ty = sequence[i + 1]

            if not (0 <= fx < BOARD_SIZE and 0 <= fy < BOARD_SIZE and
                    0 <= tx < BOARD_SIZE and 0 <= ty < BOARD_SIZE):  # проверка границ
                print(f"Out of bounds: from=({fx},{fy}), to=({tx},{ty})")
                return False

            piece = self.board[fy][fx]
            print(f"Piece at ({fx},{fy}): {piece}")
            if (self.current_turn == WHITE and piece not in [WHITE, WHITE_KING]) or (
                self.current_turn == BLACK and piece not in [BLACK, BLACK_KING]
            ):  # проверка правильной шашки
                print(f"Wrong piece: piece={piece}, current_turn={self.current_turn}")
                return False

            if self.board[ty][tx] != EMPTY:  # клетка куда шагаем должна быть пустой
                print(f"Target not empty: target=({tx},{ty}), value={self.board[ty][tx]}")
                return False

            dx, dy = tx - fx, ty - fy
            print(f"Movement: dx={dx}, dy={dy}")

            if piece in [WHITE_KING, BLACK_KING]:
                if abs(dx) != abs(dy):  # дамки могут ходить на любое расстояние по диагонали
                    print(f"Invalid king move: not diagonal, dx={dx}, dy={dy}")
                    return False
                steps = abs(dx)
                direction_x = dx // abs(dx) if dx != 0 else 0
                direction_y = dy // abs(dy) if dy != 0 else 0
                enemy_count = 0
                enemy_pos = None
                for step in range(1, steps):
                    mid_x = fx + direction_x * step
                    mid_y = fy + direction_y * step
                    if not (0 <= mid_x < BOARD_SIZE and 0 <= mid_y < BOARD_SIZE):
                        break
                    if self.board[mid_y][mid_x] != EMPTY:
                        if (self.current_turn == WHITE and self.board[mid_y][mid_x] in [BLACK, BLACK_KING]) or (
                            self.current_turn == BLACK and self.board[mid_y][mid_x] in [WHITE, WHITE_KING]
                        ):
                            if enemy_count > 0:
                                print(f"Multiple enemies in path at ({mid_x},{mid_y})")
                                return False
                            enemy_count += 1
                            enemy_pos = (mid_x, mid_y)
                        else:
                            print(f"Invalid path for king at ({mid_x},{mid_y}): {self.board[mid_y][mid_x]}")
                            return False
                if enemy_count == 1:
                    captured = True
                    mid_x, mid_y = enemy_pos
                    if abs(tx - mid_x) < 1 or abs(ty - mid_y) < 1:
                        print(f"King must land after enemy: enemy=({mid_x},{mid_y}), target=({tx},{ty})")
                        return False
                if captured and i < len(sequence) - 2:
                    next_fx, next_fy = tx, ty
                    next_tx, next_ty = sequence[i + 2]
                    next_dx, next_dy = next_tx - next_fx, next_ty - next_fy
                    if abs(next_dx) == abs(next_dy) and abs(next_dx) >= 2:
                        next_direction_x = next_dx // abs(next_dx) if next_dx != 0 else 0
                        next_direction_y = next_dy // abs(next_dy) if next_dy != 0 else 0
                        can_continue = False
                        for j in range(1, abs(next_dx)):
                            check_x = next_fx + next_direction_x * j
                            check_y = next_fy + next_direction_y * j
                            if not (0 <= check_x < BOARD_SIZE and 0 <= check_y < BOARD_SIZE):
                                break
                            if self.board[check_y][check_x] in ([BLACK, BLACK_KING] if self.current_turn == WHITE else [WHITE, WHITE_KING]):
                                can_continue = True
                                break
                        if not can_continue:
                            print(f"No further captures possible after ({tx},{ty})")
                            return False
            elif abs(dx) == 2 and abs(dy) == 2:
                mid_x, mid_y = (fx + tx) // 2, (fy + ty) // 2
                enemy_piece = self.board[mid_y][mid_x]
                print(f"Checking capture: mid=({mid_x},{mid_y}), enemy_piece={enemy_piece}")
                if enemy_piece != EMPTY and (
                    (self.current_turn == WHITE and enemy_piece in [BLACK, BLACK_KING]) or
                    (self.current_turn == BLACK and enemy_piece in [WHITE, WHITE_KING])
                ):
                    captured = True
                else:
                    print(f"Invalid capture: enemy_piece={enemy_piece}, current_turn={self.current_turn}")
                    return False
            elif abs(dx) != 1 or abs(dy) != 1:
                print(f"Invalid move distance: dx={dx}, dy={dy}")
                return False
            else:
                expected_dy = -1 if self.current_turn == WHITE else 1  # Проверяем направление хода для обычных шашек
                if dy != expected_dy:
                    print(f"Invalid direction: dy={dy}, expected={expected_dy}")
                    return False

        if self.must_continue and not captured:
            print("Must continue capturing")
            return False

        print("Move is valid")
        return True

    def make_move(self, move: Move, player: str):  # выполняем ход
        print(f"Making move for {player}: {move.sequence}")
        if not self.is_valid_move(move, player):
            raise HTTPException(status_code=400, detail="Недопустимый ход")

        if self.last_move_by == player and not self.must_continue:  # проверка на дублирующий ход от того же игрока
            raise HTTPException(status_code=400, detail="Не ваш ход")

        sequence = move.sequence
        captured = False

        for i in range(len(sequence) - 1):
            fx, fy = sequence[i]
            tx, ty = sequence[i + 1]

            self.board[ty][tx] = self.board[fy][fx]
            self.board[fy][fx] = EMPTY

            dx, dy = tx - fx, ty - fy
            if abs(dx) >= 2 and abs(dy) >= 2 and abs(dx) == abs(dy):
                direction_x = dx // abs(dx) if dx != 0 else 0
                direction_y = dy // abs(dy) if dy != 0 else 0
                steps = abs(dx)
                for step in range(1, steps):
                    mid_x = fx + direction_x * step
                    mid_y = fy + direction_y * step
                    if self.board[mid_y][mid_x] != EMPTY:
                        captured_piece = self.board[mid_y][mid_x]
                        self.board[mid_y][mid_x] = EMPTY
                        captured = True
                        if captured_piece in [WHITE, WHITE_KING]:
                            self.eaten_white_pieces.append(captured_piece)
                        elif captured_piece in [BLACK, BLACK_KING]:
                            self.eaten_black_pieces.append(captured_piece)
                        print(f"Captured piece {captured_piece} at ({mid_x},{mid_y})")
                        break  # цдаляем только одну шашку

            self.promote_to_king(tx, ty)

            if captured and self.can_continue_capture(tx, ty):
                self.must_continue = True
                self.last_moved_piece = (tx, ty)
                print(f"Must continue capturing from ({tx},{ty})")
            else:
                self.must_continue = False
                self.last_moved_piece = None
                self.current_turn = BLACK if self.current_turn == WHITE else WHITE
                print(f"Turn switched to {self.current_turn}")

        self.last_move_by = player
        print(f"Move completed by {player}, new turn: {self.current_turn}")

        winner = self.check_end_game()
        if winner:
            self.game_ended = True
            print(f"Game ended, winner: {winner}")

        return winner

    def can_continue_capture(self, x, y):  # проверка возможности продолжения взятия
        piece = self.board[y][x]
        if piece not in [WHITE_KING, BLACK_KING]:
            return any(
                self.is_valid_move(Move(sequence=[(x, y), (x + dx * 2, y + dy * 2)]),
                                   self.white_name if self.current_turn == WHITE else self.black_name)
                for dx, dy in [(-1, -1), (-1, 1), (1, -1), (1, 1)]
            )
        else:
            directions = [(-1, -1), (-1, 1), (1, -1), (1, 1)]
            for dx, dy in directions:
                k = 1
                found_enemy = False
                while 0 <= x + k * dx < BOARD_SIZE and 0 <= y + k * dy < BOARD_SIZE:
                    check_x = x + k * dx
                    check_y = y + k * dy
                    if self.board[check_y][check_x] != EMPTY:
                        if (
                            (self.current_turn == WHITE and self.board[check_y][check_x] in [BLACK, BLACK_KING]) or
                            (self.current_turn == BLACK and self.board[check_y][check_x] in [WHITE, WHITE_KING])
                        ):
                            if found_enemy:
                                break
                            found_enemy = True
                        else:
                            break
                    if found_enemy:
                        m = k + 1
                        while 0 <= x + m * dx < BOARD_SIZE and 0 <= y + m * dy < BOARD_SIZE:
                            if self.board[y + m * dy][x + m * dx] == EMPTY:
                                print(f"Can continue capture from ({x},{y}) to ({x + m * dx},{y + m * dy})")
                                return True
                            m += 1
                        break
                    k += 1
            print(f"No further captures from ({x},{y})")
            return False

    def check_end_game(self):  # проверка окончания игры
        white_pieces = sum(row.count(WHITE) + row.count(WHITE_KING) for row in self.board)
        black_pieces = sum(row.count(BLACK) + row.count(BLACK_KING) for row in self.board)
        if white_pieces == 0:
            return "BLACK"
        elif black_pieces == 0:
            return "WHITE"
        return None

@sio.event
async def connect(sid, environ, auth=None):
    print(f"Raw environ: {environ}")
    print(f"Received auth: {auth}")
    token = auth.get('token') if auth else None
    origin = environ.get('HTTP_ORIGIN')
    headers = environ.get('HTTP_HEADERS', {})
    print(f"New connection attempt: sid={sid}, environ={environ}")
    print(f"Token: {token}, Origin: {origin}, Headers: {headers}")
    if not token:
        print(f"No token provided for sid: {sid}")
        await sio.disconnect(sid)
        return False
    try:
        user_manager.check_token(token)
        print(f"Token valid for sid: {sid}")
        return True
    except HTTPException as e:
        print(f"Token validation failed for sid {sid}: {e.detail}")
        await sio.disconnect(sid)
        return False

@sio.event
async def disconnect(sid):
    print(f"Client disconnected: {sid}")  # отладка
    for game_id, conn in list(connections.items()):
        if conn.get('white') == sid or conn.get('black') == sid:
            if game_id in games:
                game = games[game_id]
                username = game.white_name if conn.get('white') == sid else game.black_name
                if conn.get('white') == sid:
                    game.white_name = None
                    conn['white'] = None
                else:
                    game.black_name = None
                    conn['black'] = None
                await sio.emit('playerLeft', {'username': username}, room=game_id)  # уведомляем оставшегося игрока (нереализовано на клиенте -> прерывание игры без уведомления)
                if not game.white_name and not game.black_name:
                    del games[game_id]
                    del connections[game_id]
                    await sio.emit('roomDeleted', game_id, room=game_id)
                    print(f"Game {game_id} deleted because it is empty")
                else:
                    print(f"Player {username} left game {game_id}")
            break

@sio.on('create_room')
async def create_room(sid, data):
    username = data.get('username')
    token = data.get('token')
    print(f"Create room request from {username} with token {token[:10]}...")

    try:
        current_user = user_manager.check_token(token)  # проверяем токен
        game_id = secrets.token_hex(8)
        games[game_id] = CheckersGame()
        games[game_id].white_name = current_user
        connections[game_id] = {'white': sid, 'black': None}
        await sio.enter_room(sid, game_id)  # добавляем белого игрока в комнату
        print(f"Game created: {game_id} by {current_user}, white player added to room")

        await sio.emit('room_created', {
            'game_id': game_id,
            'name': f'Комната {current_user}',
            'players': 1,
            'status': 'waiting'
        }, to=sid)  # отправляем уведомление создателю комнаты
        print(f"Room created event sent to sid: {sid}")

        rooms = [
            {
                'id': gid,
                'name': f'Комната {game.white_name}',
                'players': 1 if not game.black_name else 2,
                'status': 'waiting' if not game.black_name else 'playing'
            }
            for gid, game in games.items()
            if not game.black_name and not game.game_ended
        ]
        for conn_sid in sio.manager.get_participants('/'):
            await sio.emit('rooms_list', {'rooms': rooms}, to=conn_sid)  # Обновляем список комнат для всех клиентов
        print(f"Broadcasted rooms list to all clients: {rooms}")

    except Exception as e:
        print(f"Error creating room: {str(e)}")
        await sio.emit('game_error', {'message': str(e)}, to=sid)

@sio.on('join_game')
async def join_game(sid, data):
    game_id = data.get('game_id')
    token = data.get('token')  # токен для авторизации
    username = data.get('username')
    print(f"Join game attempt: sid={sid}, game_id={game_id}, token={token[:10]}...")

    if not game_id or game_id not in games:
        await sio.emit('game_error', {'message': 'Game not found'}, to=sid)
        print(f"Game {game_id} not found for sid: {sid}")
        return

    try:
        username = user_manager.check_token(token)
        game = games[game_id]
        if game.black_name or game.game_ended:
            await sio.emit('game_error', {'message': 'Game is full or ended'}, to=sid)
            print(f"Game {game_id} is full or ended for sid: {sid}")
            return
        connections[game_id]['black'] = sid
        game.black_name = username
        await sio.enter_room(sid, game_id)
        print(f"User {username} joined game: {game_id}")  # отладка
        player_color = 'black'
        board_for_black = game.get_board(player_color)
        print(f"Sending board to black player (sid: {sid}): {board_for_black}")  # Отладка доски
        await sio.emit('game_joined', {
            'game_id': game_id,
            'board': board_for_black,
            'turn': 'WHITE',
            'white_name': game.white_name,
            'black_name': game.black_name,
            'message': 'Joined game',
            'player_color': player_color,
            'must_continue': game.must_continue,
            'last_moved_piece': game.last_moved_piece,
            'eaten_white_pieces': game.eaten_white_pieces,
            'eaten_black_pieces': game.eaten_black_pieces,
            'game_ended': game.game_ended
        }, to=sid)  # отправляем напрямую чёрному игроку
        if connections[game_id].get('white'):
            board_for_white = game.get_board('white')
            print(f"Sending board to white player (sid: {connections[game_id]['white']}): {board_for_white}")  # отладка доски
            await sio.emit('game_joined', {
                'game_id': game_id,
                'board': board_for_white,
                'turn': 'WHITE',
                'white_name': game.white_name,
                'black_name': game.black_name,
                'message': 'Opponent joined',
                'player_color': 'white',
                'must_continue': game.must_continue,
                'last_moved_piece': game.last_moved_piece,
                'eaten_white_pieces': game.eaten_white_pieces,
                'eaten_black_pieces': game.eaten_black_pieces,
                'game_ended': game.game_ended
            }, to=connections[game_id]['white'])
        await sio.emit('playerJoined', {'username': username}, room=game_id)  # уведомляем о присоединении
        print(f"Game joined event sent to room: {game_id}")
    except HTTPException as e:
        print(f"Join game error: {e.detail}")
        await sio.emit('game_error', {'message': str(e.detail)}, to=sid)

@sio.on('make_move')
async def make_move(sid, data):
    game_id = data.get('game_id')
    move_data = data.get('move')
    token = data.get('token')

    print(f"Received move request: sid={sid}, game_id={game_id}, move={move_data}")

    if not game_id or game_id not in games:
        await sio.emit('game_error', {'message': 'Game not found'}, to=sid)
        print(f"Game {game_id} not found for sid: {sid}")
        return

    try:
        username = user_manager.check_token(token)  # проверяем токен
        game = games[game_id]
        if connections[game_id].get('white') == sid:
            if game.white_name != username:
                await sio.emit('game_error', {'message': 'Invalid user for white player'}, to=sid)
                print(f"Invalid user for white player: {username} != {game.white_name}")
                return
        elif connections[game_id].get('black') == sid:
            if game.black_name != username:
                await sio.emit('game_error', {'message': 'Invalid user for black player'}, to=sid)
                print(f"Invalid user for black player: {username} != {game.black_name}")
                return
        else:
            await sio.emit('game_error', {'message': 'Not a player in this game'}, to=sid)
            print(f"User with sid {sid} is not a player in game {game_id}")
            return

        move = Move(**move_data)
        print(f"Attempting move: {move.sequence} by {username}")
        winner = game.make_move(move, username)
        message = "Ход выполнен" if not winner else f"Игра окончена! Победитель: {winner}"
        for conn_sid, color in [(connections[game_id].get('white'), 'white'),
                                (connections[game_id].get('black'), 'black')]:
            if conn_sid:
                board = game.get_board(color)
                print(
                    f"Sending game_update to {color} player (sid {conn_sid}): board={board}, turn={game.current_turn}")
                await sio.emit('game_update', {
                    'board': board,
                    'turn': 'WHITE' if game.current_turn == WHITE else 'BLACK',
                    'white_name': game.white_name,
                    'black_name': game.black_name,
                    'message': message,
                    'player_color': color,
                    'eaten_white_pieces': game.eaten_white_pieces,
                    'eaten_black_pieces': game.eaten_black_pieces,
                    'must_continue': game.must_continue,
                    'last_moved_piece': game.last_moved_piece,
                    'game_ended': game.game_ended
                }, to=conn_sid)  # отправляем доску с учётом перспективы каждого игрока
        print(f"Move made in game {game_id}, updated state sent to room")
        if winner:
            for conn_sid, color in [(connections[game_id].get('white'), 'white'),
                                    (connections[game_id].get('black'), 'black')]:
                if conn_sid:
                    board = game.get_board(color)
                    await sio.emit('game_ended', {
                        'winner': winner,
                        'board': board,
                        'eaten_white_pieces': game.eaten_white_pieces,
                        'eaten_black_pieces': game.eaten_black_pieces
                    }, to=conn_sid)
            print(f"Game {game_id} ended, winner: {winner}, game_ended event sent")
    except HTTPException as e:
        await sio.emit('game_error', {'message': str(e.detail)}, to=sid)
        print(f"Move error in game {game_id}: {e.detail}")

@sio.on('leaveRoom')
async def leave_room(sid, data):
    game_id = data.get('game_id')
    token = data.get('token')
    print(f"Leave room request: sid={sid}, game_id={game_id}")
    if not game_id or game_id not in games:
        await sio.emit('game_error', {'message': 'Game not found'}, to=sid)
        print(f"Game {game_id} not found for sid: {sid}")
        return

    try:
        username = user_manager.check_token(token)
        game = games.get(game_id)
        if not game:
            await sio.emit('game_error', {'message': 'Game not found'}, to=sid)
            print(f"Game {game_id} not found for sid: {sid}")
            return
        if connections[game_id].get('white') == sid:
            if game.white_name != username:
                await sio.emit('game_error', {'message': 'Invalid user for white player'}, to=sid)
                print(f"Invalid user for white player: {username} != {game.white_name}")
                return
            game.white_name = None
            connections[game_id]['white'] = None
        elif connections[game_id].get('black') == sid:
            if game.black_name != username:
                await sio.emit('game_error', {'message': 'Invalid user for black player'}, to=sid)
                print(f"Invalid user for black player: {username} != {game.black_name}")
                return
            game.black_name = None
            connections[game_id]['black'] = None
        else:
            await sio.emit('game_error', {'message': 'Not a player in this game'}, to=sid)
            print(f"User with sid {sid} is not a player in game {game_id}")
            return

        await sio.emit('playerLeft', {'username': username}, room=game_id)

        if not game.white_name and not game.black_name:
            del games[game_id]
            del connections[game_id]
            await sio.emit('roomDeleted', game_id, room=game_id)
            print(f"Game {game_id} deleted because it is empty")
        else:
            if not game.game_ended:
                # если игра не закончена, второй игрок остаётся в ожидании (убрать в процессе)
                game.current_turn = WHITE
                game.must_continue = False
                game.last_moved_piece = None
                for conn_sid, color in [(connections[game_id].get('white'), 'white'),
                                        (connections[game_id].get('black'), 'black')]:
                    if conn_sid:
                        board = game.get_board(color)
                        await sio.emit('game_update', {
                            'board': board,
                            'turn': 'WHITE',
                            'white_name': game.white_name,
                            'black_name': game.black_name,
                            'message': f'Игрок {username} покинул игру',
                            'player_color': color,
                            'eaten_white_pieces': game.eaten_white_pieces,
                            'eaten_black_pieces': game.eaten_black_pieces,
                            'must_continue': game.must_continue,
                            'last_moved_piece': game.last_moved_piece,
                            'game_ended': game.game_ended
                        }, to=conn_sid)

        rooms = [
            {
                'id': gid,
                'name': f'Комната {game.white_name}',
                'players': 1 if not game.black_name else 2,
                'status': 'waiting' if not game.black_name else 'playing'
            }
            for gid, game in games.items()
            if not game.black_name and not game.game_ended
        ]
        for conn_sid in sio.manager.get_participants('/'):
            await sio.emit('rooms_list', {'rooms': rooms}, to=conn_sid)  # рассылка обновленного списка комнат
        print(f"Broadcasted rooms list to all clients: {rooms}")
    except HTTPException as e:
        await sio.emit('game_error', {'message': str(e.detail)}, to=sid)
        print(f"Leave room error: {e.detail}")

@sio.on('check_connection')
async def check_connection(sid):
    print(f"Connection check received from sid: {sid}")
    await sio.emit('connection_confirmed', {'message': 'Connection successful'}, to=sid)
    print(f"Connection confirmed sent to sid: {sid}")

@sio.on('get_rooms')
async def get_rooms(sid, data):
    token = data.get('token')
    try:
        user_manager.check_token(token)
        rooms = [
            {
                'id': gid,
                'name': f'Комната {game.white_name}',
                'players': 1 if not game.black_name else 2,
                'status': 'waiting' if not game.black_name else 'playing'
            }
            for gid, game in games.items()
            if not game.black_name and not game.game_ended
        ]
        await sio.emit('rooms_list', {'rooms': rooms}, to=sid)
        print(f"Sent rooms list to sid {sid}: {rooms}")
    except HTTPException as e:
        await sio.emit('game_error', {'message': str(e.detail)}, to=sid)
        print(f"Get rooms error for sid {sid}: {e.detail}")

@app.get("/rooms")
async def get_rooms(current_user: str = Depends(token_checker)):  # получаем список доступных комнат
    rooms = [
        {
            "id": game_id,
            "name": f"Комната {game.white_name}",
            "players": 1 if not game.black_name else 2,
            "status": "waiting" if not game.black_name else "playing"
        }
        for game_id, game in games.items()
        if not game.black_name and not game.game_ended
    ]
    return {"rooms": rooms}

@app.post("/moveGetGame")
async def move_get_game(game_id: str, move: Optional[Move] = None,
                        current_user: str = Depends(token_checker)):  # выполняем ход и получаем игру
    if game_id not in games:
        raise HTTPException(status_code=404, detail="Игра не найдена")

    game = games[game_id]
    if current_user not in (game.white_name, game.black_name):
        raise HTTPException(status_code=403, detail="Вы не участник этой игры")

    message = "Состояние игры получено"
    player_color = 'white' if current_user == game.white_name else 'black'
    if move:
        try:
            winner = game.make_move(move, current_user)
            if winner:
                message = f"Игра окончена! Победитель: {winner}"
                for conn_sid, color in [(connections[game_id].get('white'), 'white'),
                                        (connections[game_id].get('black'), 'black')]:
                    if conn_sid:
                        await sio.emit('game_update', {
                            'board': game.get_board(color),
                            'turn': 'WHITE' if game.current_turn == WHITE else 'BLACK',
                            'white_name': game.white_name,
                            'black_name': game.black_name,
                            'message': message,
                            'player_color': color,
                            'eaten_white_pieces': game.eaten_white_pieces,
                            'eaten_black_pieces': game.eaten_black_pieces,
                            'must_continue': game.must_continue,
                            'last_moved_piece': game.last_moved_piece,
                            'game_ended': game.game_ended
                        }, to=conn_sid)
                        await sio.emit('game_ended', {
                            'winner': winner,
                            'board': game.get_board(color),
                            'eaten_white_pieces': game.eaten_white_pieces,
                            'eaten_black_pieces': game.eaten_black_pieces
                        }, to=conn_sid)
                print(f"Game {game_id} ended via HTTP, winner: {winner}")
            else:
                message = "Ход выполнен"
                for conn_sid, color in [(connections[game_id].get('white'), 'white'),
                                        (connections[game_id].get('black'), 'black')]:
                    if conn_sid:
                        await sio.emit('game_update', {
                            'board': game.get_board(color),
                            'turn': 'WHITE' if game.current_turn == WHITE else 'BLACK',
                            'white_name': game.white_name,
                            'black_name': game.black_name,
                            'message': message,
                            'player_color': color,
                            'eaten_white_pieces': game.eaten_white_pieces,
                            'eaten_black_pieces': game.eaten_black_pieces,
                            'must_continue': game.must_continue,
                            'last_moved_piece': game.last_moved_piece,
                            'game_ended': game.game_ended
                        }, to=conn_sid)
                print(f"Move made via HTTP in game {game_id}")
        except HTTPException as e:
            return format_response(status=1, error=str(e.detail))

    return format_response(data={
        "board": game.get_board(player_color),
        "turn": "WHITE" if game.current_turn == WHITE else "BLACK",
        "whiteName": game.white_name,
        "blackName": game.black_name,
        "message": message,
        "player_color": player_color,
        "must_continue": game.must_continue,
        "last_moved_piece": game.last_moved_piece,
        "eaten_white_pieces": game.eaten_white_pieces,
        "eaten_black_pieces": game.eaten_black_pieces,
        "game_ended": game.game_ended
    })

@app.post("/register")
async def register(data: UserRegister):  # регистрируем пользователя
    return user_manager.register_new_user(data.username, data.email, data.password)

@app.post("/login")
async def login(data: AuthData):  # авторизуем пользователя
    return user_manager.login_user(data.username_or_email, data.password)

@app.get("/me")
async def get_user(current_user: str = Depends(token_checker)):  # получаем текущего пользователя
    return {"message": f"Пользователь {current_user}"}

@app.post("/requestPasswordReset")
async def request_password_reset(data: PasswordResetRequest):  # запрос сброса пароля
    return user_manager.request_password_reset(data.email)

@app.post("/resetPassword")
async def reset_password(data: PasswordReset):  # сброс пароля
    return user_manager.reset_password(data.reset_token, data.new_password)