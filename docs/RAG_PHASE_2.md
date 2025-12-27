# РАГ фаза 2

Короткая фиксация договоренностей по Iteration 2 (ингест + суммаризация).

## 1) Сырые сообщения для суммаризации
Храним только реальные тексты пользователя и ответы, которые он получил в Telegram.
Tool-calls и промежуточные системные сообщения не пишем, чтобы не засорять корпус.

### Таблица rag_messages (новая)
Минимальный набор полей:
- id (PK)
- user_id (FK → users.id)
- project_id (FK → projects.id, NULL если не определен)
- role: "user" | "assistant"
- message_text (Text)
- message_id (ID сообщения в Telegram, если есть)
- source: "telegram"
- created_at

### Поток данных
1. Telegram inbound → API пишет запись role=user.
2. Telegram outbound (ответ бота) → API пишет запись role=assistant.
3. Суммаризатор в scheduler читает несуммаризированные сообщения пользователя
   (один user = один тред), считает порог и пишет summary в rag_conversation_summaries.

Порог суммаризации вынести в настройки (число токенов или символов).

## 2) Webhook payload от service-template
Цель: простой хук, который присылает готовые тексты доков и метаданные.

### Пример payload
```json
{
  "event": "rag.docs.upsert",
  "project_id": "project-123",
  "user_id": 42,
  "repo": {
    "full_name": "org/repo",
    "ref": "refs/heads/main",
    "commit_sha": "abc123"
  },
  "documents": [
    {
      "source_type": "repo_doc",
      "source_id": "README.md",
      "source_uri": "repo://org/repo/README.md",
      "scope": "project",
      "path": "README.md",
      "content": "text here",
      "language": "ru",
      "updated_at": "2026-01-01T12:00:00Z",
      "content_hash": "sha256:..."
    }
  ]
}
```

### Аутентификация
HMAC подпись тела запроса.
- Header `X-RAG-Timestamp` (unix epoch)
- Header `X-RAG-Signature`: `sha256=<hex>`
- Подпись: HMAC-SHA256(secret, "{timestamp}.{raw_body}")
- Сервер проверяет допустимый сдвиг времени (например, 5 минут).

Secret хранится в настройках API (например, `RAG_INGEST_SECRET`).

## 3) Подсчет токенов
Используем `tiktoken` и базовую схему (например, `cl100k_base`) без
сложных эвристик. Для v1 достаточно простого подсчета.

## 4) Public corpus
Публичный корпус — документация оркестратора.
На старте индексируем только README репозитория оркестратора (scope=public),
позже расширяем на другие docs.

## 5) Триггеры обновления
- GitHub webhook от service-template для project scope.
- Суммаризация при превышении порога сообщений/токенов по конкретному user_id.
- Отдельный план на обновление public corpus (пока вручную/по cron).
