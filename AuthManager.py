from fastapi import FastAPI, HTTPException, Header
from dotenv import load_dotenv
import json
import os
import jwt
from datetime import datetime, timedelta
from passlib.context import CryptContext
from typing import Optional
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import secrets
from email_validator import validate_email, EmailNotValidError

load_dotenv()

USERS_FILE = "users.json"  # файл, где будем хранить пользователей
# настройки для jwt токенов
TOKEN_SECRET = "hidden-gem-key"  # секретный ключ для токенов
TOKEN_ALGO = "HS256"  # алгоритм для подписи токенов
TOKEN_TIME = 60  # время действия токена

class UserManager:
    def __init__(self):
        self.password_tool = CryptContext(schemes=["bcrypt"], deprecated="auto")  # настраиваем инструмент для хеширования паролей
        self.users = self.load_users_from_file()  # загружаем пользователей из файла
        self.smtp_server = "smtp.yandex.com"  # настраиваем smtp для яндекса
        self.smtp_port = 465  # порт для ssl
        self.smtp_user = os.getenv("SMTP_USER", "checkers.assistant@yandex.com")  # email яндекса
        self.smtp_password = os.getenv("SMTP_PASSWORD", "gkvutwzqwenahdza")  # пароль приложения яндекса
        print(f"SMTP Config: user={self.smtp_user}, password={self.smtp_password[:4]}..., port={self.smtp_port}")  # отладка

    def load_users_from_file(self):  # для загрузки пользователей из файла
        if os.path.exists(USERS_FILE):  # есть ли файл
            with open(USERS_FILE, "r") as file:  # открываем файл и читаем данные
                try:
                    return json.load(file)
                except json.JSONDecodeError:
                    return {}
        return {}  # если файла нет, возвращаем пустой словарь

    def save_users_to_file(self):  # для сохранения пользователей в файл
        with open(USERS_FILE, "w") as file:  # открываем файл и записываем данные
            json.dump(self.users, file)

    def make_password_hash(self, password):  # хеширование пароля
        return self.password_tool.hash(password)  # хешируем пароль

    def check_password(self, password, hashed_password):  # проверка пароля
        return self.password_tool.verify(password, hashed_password)  # совпадает ли пароль с хешем

    def register_new_user(self, username, email, password):  # регистрация нового пользователя
        if username in self.users:  # нет ли такого уже
            raise HTTPException(status_code=400, detail="Пользователь уже существует")
        try:
            validate_email(email, check_deliverability=False)
        except EmailNotValidError:
            raise HTTPException(status_code=400, detail="Недействительный email")
        if any(user["email"] == email for user in self.users.values()):
            raise HTTPException(status_code=400, detail="Email уже зарегистрирован")

        hashed_password = self.make_password_hash(password)  # хешируем пароль
        self.users[username] = {
            "password": hashed_password,
            "email": email,
            "reset_token": None  # для токена сброса пароля
        }
        self.save_users_to_file()  # сохраняем изменения в файл
        return {"message": "Регистрация успешна"}

    def login_user(self, username_or_email, password):  # вход пользователя
        username = None
        if username_or_email in self.users: # является ли вход по username или email
            username = username_or_email
        else:
            # Ищем пользователя по email
            for user_name, user_data in self.users.items():
                if user_data["email"] == username_or_email:
                    username = user_name
                    break

        if username is None:  # пользователь не найден
            raise HTTPException(status_code=400, detail="Неверный логин или email")
        if not self.check_password(password, self.users[username]["password"]):  # проверяем пароль
            raise HTTPException(status_code=400, detail="Неверный пароль")

        token_expires = timedelta(minutes=TOKEN_TIME)  # создаём токен
        token = self.create_new_token({"sub": username_or_email}, token_expires)
        return {
            "message": "Вход выполнен",
            "access_token": token,
            "token_type": "bearer",
            "username": username
        }  # возвращаем токен и сообщение

    def create_new_token(self, data, expires_delta):  # функция для создания jwt токена
        token_data = data.copy()  # копируем данные
        expire_time = datetime.utcnow() + expires_delta  # устанавливаем время истечения токена
        token_data["exp"] = expire_time  # добавляем время истечения в данные
        new_token = jwt.encode(token_data, TOKEN_SECRET, algorithm=TOKEN_ALGO)  # создаём токен
        return new_token

    def check_token(self, token):  # функция для проверки токена и получения пользователя
        try:
            payload = jwt.decode(token, TOKEN_SECRET, algorithms=[TOKEN_ALGO])  # пробуем декодировать токен
            identifier = payload.get("sub")  # получаем идентификатор (имя или email)
            if identifier is None:  # проверяем, есть ли sub
                raise HTTPException(status_code=401, detail="Недействительный токен")
            if identifier in self.users:  # если это имя пользователя
                return identifier
            # Проверяем, является ли sub email
            for username, user_data in self.users.items():
                if user_data["email"] == identifier:
                    return username
            raise HTTPException(status_code=401, detail="Недействительный токен")
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Срок действия токена истёк")
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="Недействительный токен")

    def send_reset_email(self, email, username, reset_token):  # отправка письма для сброса пароля
        msg = MIMEMultipart()
        msg["From"] = self.smtp_user
        msg["To"] = email
        msg["Subject"] = "Сброс пароля для игры в шашки"
        body = f"""
        Здравствуйте, {username}!
        Вы запросили сброс пароля. Используйте этот токен для сброса: {reset_token}
        Если вы не запрашивали сброс, проигнорируйте это письмо.
        """
        msg.attach(MIMEText(body, "plain"))  # добавляем текст
        try:
            print(f"Sending email to {email} with token {reset_token}")  # отладка
            server = smtplib.SMTP_SSL(self.smtp_server, self.smtp_port)  # подключаемся к серверу
            server.login(self.smtp_user, self.smtp_password)  # авторизуемся
            server.sendmail(self.smtp_user, email, msg.as_string())  # отправляем письмо
            server.quit()  # закрываем соединение
        except Exception as e:
            print(f"SMTP Error: {str(e)}")  # отладка
            raise HTTPException(status_code=500, detail=f"Ошибка отправки email: {str(e)}")

    def request_password_reset(self, email):  # запрос сброса пароля
        for username, user_data in self.users.items():  # ищем пользователя
            if user_data["email"] == email:
                reset_token = secrets.token_urlsafe(32)  # создаём токен
                user_data["reset_token"] = {
                    "token": reset_token,
                    "expires": (datetime.utcnow() + timedelta(hours=1)).isoformat()
                }
                self.save_users_to_file()  # обновляем файл
                self.send_reset_email(email, username, reset_token)  # отправляем письмо
                return {"message": "Письмо для сброса пароля отправлено"}
        raise HTTPException(status_code=404, detail="Email не найден")

    def reset_password(self, reset_token, new_password):  # сброс пароля
        for username, user_data in self.users.items(): # ищем токен
            if user_data.get("reset_token") and user_data["reset_token"]["token"] == reset_token:
                expiration = datetime.fromisoformat(user_data["reset_token"]["expires"])  # проверяем срок действия
                if datetime.utcnow() > expiration:
                    raise HTTPException(status_code=400, detail="Токен сброса пароля истёк")
                user_data["password"] = self.make_password_hash(new_password)  # обновляем пароль
                user_data["reset_token"] = None  # удаляем токен
                self.save_users_to_file()  # сохраняем файл
                return {"message": "Пароль успешно сброшен"}
        raise HTTPException(status_code=400, detail="Недействительный токен сброса")

def token_checker(authorization: Optional[str] = Header(None)) -> str:  # проверка токена из заголовка authorization
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Укажите токен в заголовке Authorization")
    token = authorization.split(" ")[1]
    return UserManager().check_token(token)