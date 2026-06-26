# 🚀 Menza Communication App Backend

> A scalable, real-time messaging and community platform backend built with Django, Django REST Framework, and Django Channels.

## 📖 Overview

The Menza Communication App Backend is a comprehensive RESTful API and WebSocket service designed to power a modern, real-time communication platform. It features robust user authentication, 1-on-1 direct messaging, group chats, and public/private discoverable channels. 

Engineered with scalability and security in mind, this backend leverages asynchronous WebSockets for live interactions, cursor pagination for optimal data fetching, and enterprise-grade security practices including Argon2 hashing and JWT authentication.

## ✨ Key Features

- **Real-Time Communication**: Powered by Django Channels and Redis to handle live typing indicators, read receipts, and instant message delivery.
- **Robust Authentication**: Multi-step registration with OTP verification via email/phone, stateless JWT authentication, and secure password management using Argon2id.
- **Direct Messaging (DMs)**: 1-on-1 private messaging with features like pinning, archiving, muting, and full message history management.
- **Group Chats**: Private and public groups with granular role-based access control (Admin/Member permissions) and moderation tools.
- **Channels & Communities**: Discoverable channels supporting rich posts, threaded comments, emoji reactions, and paid visibility boosting via webhooks.
- **Performance Optimized**: Cursor-based pagination across all heavy endpoints (messages, posts, feeds) to ensure smooth client rendering and low database overhead.

## 🛠️ Technology Stack

- **Core Framework**: Python 3, Django 4.2+, Django REST Framework
- **Real-Time / WebSockets**: Django Channels, Daphne, Redis
- **Database**: MySQL (optimized with `mysqlclient`)
- **Authentication**: JWT (JSON Web Tokens) via `djangorestframework-simplejwt`, Argon2id
- **Storage**: Cloudflare R2 / AWS S3 (`django-storages`, `boto3`)
- **Testing**: Pytest (`pytest-django`), Factory Boy
- **Environment Management**: `python-decouple`

## 🏗️ Architecture Modules

The project is structured into highly cohesive, decoupled Django apps:

1. `authapp/`: Handles everything from JWT tokens to user profiles, contact synchronization, and user blocking.
2. `messageapp/`: Manages direct 1-on-1 conversations, message delivery, read status, and media attachments.
3. `groupapp/`: Controls multi-user environments, including role-based permissions and group message broadcasting.
4. `channelapp/`: Powers the community aspect, including posts, reactions, comments, subscriptions, and channel discovery.

## 🚀 Getting Started

### Prerequisites
- Python 3.10+
- MySQL Server
- Redis (for Channels)

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/Tasin44/Menza_Communication_APP_backend.git
   cd Menza_Communication_APP_backend
   ```

2. **Set up a virtual environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure Environment Variables:**
   Create a `.env` file in the root directory based on the project requirements (DB credentials, Redis URL, JWT Secret, etc.).

5. **Run Migrations:**
   ```bash
   python manage.py migrate
   ```

6. **Start the Development Server:**
   This project uses Daphne for ASGI (handling both HTTP and WebSockets).
   ```bash
   python manage.py runserver 
   # or run daphne directly for full ASGI support
   ```

## 📚 API Documentation

A comprehensive API breakdown is available in `api_documentation.txt`, covering 40+ endpoints including:
- `POST /api/auth/login/` (JWT generation)
- `GET /api/messages/conversations/` (Cursor-paginated DMs)
- `POST /api/channels/<id>/posts/` (Channel feed management)
- `GET /api/groups/<id>/messages/` (Group chat history)

## 🧪 Testing

The codebase maintains high reliability through automated testing.
```bash
pytest
```

---
*This repository showcases advanced backend development skills, focusing on real-time data flow, relational database design, API security, and scalable architecture.*
