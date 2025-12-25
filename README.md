# Codegen Orchestrator

Мультиагентный оркестратор на базе LangGraph для автоматической генерации и деплоя проектов.

**Вход**: Описание проекта в Телеграме  
**Выход**: Работающий проект в продакшене (код, CI/CD, домен, SSL)

## Философия

- **Автономность**: Человек заходит раз в несколько дней, смотрит отчёты, докидывает деньги
- **Агенты как узлы графа**: Каждый агент — специалист со своими инструментами
- **Нелинейность**: Агенты могут вызывать друг друга в любом порядке
- **Spec-first**: Используем [service-template](https://github.com/vladmesh/service-template) для генерации кода

## Архитектура

```
┌─────────────────────────────────────────────────────────────────┐
│                        Telegram Bot                             │
│                     (интерфейс пользователя)                    │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                       LangGraph Orchestrator                    │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐ │
│  │ Брейн-   │  │Архитек-  │  │ Разра-   │  │   Тестиров-      │ │
│  │ сторм    │◄─►  тор     │◄─►ботчик   │◄─►    щик            │ │
│  └──────────┘  └──────────┘  └──────────┘  └──────────────────┘ │
│       ▲              ▲              ▲              ▲            │
│       │              │              │              │            │
│       ▼              ▼              ▼              ▼            │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────────────────┐   │
│  │  Завхоз  │  │  DevOps  │  │      Документатор            │   │
│  │(ресурсы) │  │ (инфра)  │  │                              │   │
│  └──────────┘  └──────────┘  └──────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                    │                    │
                    ▼                    ▼
        ┌───────────────────┐   ┌───────────────────┐
        │  service-template │   │    prod_infra     │
        │   (кодогенерация) │   │   (Ansible)       │
        └───────────────────┘   └───────────────────┘
```

## Связанные проекты

| Проект | Описание | Репо |
|--------|----------|------|
| **service-template** | Spec-first фреймворк для генерации микросервисов | [GitHub](https://github.com/vladmesh/service-template) |
| **prod_infra** | Ansible playbooks для настройки серверов | [GitHub](https://github.com/vladmesh/prod_infra) |
| **Ресурсница** | Хранение и выдача ключей, токенов, доменов | *TODO* |

## Инфраструктура

- **LangGraph сервер**: Отдельный сервер для оркестратора и агентов
- **Prod серверы**: Управляются через prod_infra, используются для деплоя сгенерированных проектов
- **Телеграм**: Основной интерфейс для взаимодействия с человеком

## Development Setup

### Prerequisites
- Docker & Docker Compose
- Python 3.12+
- Git

### Quick Start

1. **Clone the repository**
   ```bash
   git clone https://github.com/vladmesh/codegen_orchestrator.git
   cd codegen_orchestrator
   ```

2. **Install pre-commit hooks** (for code quality)
   ```bash
   pip install pre-commit
   pre-commit install
   ```

3. **Set up environment**
   ```bash
   cp .env.example .env
   # Edit .env with your credentials
   ```

4. **Start services**
   ```bash
   make up
   make migrate
   make seed
   ```

5. **Run tests**
   ```bash
   make test-unit  # Fast unit tests
   make test-all   # All tests
   ```

### Development Workflow

- **Code quality**: Pre-commit hooks automatically format and lint code on commit
- **Testing**: Write tests in `services/{service}/tests/{unit,integration}/`
- **CI/CD**: GitHub Actions runs tests on every push/PR

See [TESTING.md](docs/TESTING.md) for detailed testing guide.

## Документация

- [AGENTS.md](AGENTS.md) — описание каждого агента
- [ARCHITECTURE.md](ARCHITECTURE.md) — техническая архитектура

## Roadmap

1. **Фаза 0**: Структура проекта, state schema, минимальный граф
2. **Фаза 1**: Вертикальный слайс (Телеграм → генерация бота → деплой)
3. **Фаза 2**: Горизонтальное расширение (разработчик, тестировщик, сложные проекты)
4. **Фаза 3**: Автономная работа с отчётами