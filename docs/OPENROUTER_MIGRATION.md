# Анализ миграции на OpenRouter

## 📊 Текущая ситуация

### Где используется OpenAI

Проанализировал проект и нашел следующие места использования OpenAI:

#### 1. **BaseAgentNode** (`services/langgraph/src/nodes/base.py`)
Основной класс, от которого наследуются все агенты:
```python
from langchain_openai import ChatOpenAI

async def get_llm_with_tools(self):
    config = await self.get_config()
    llm = ChatOpenAI(
        model=config.get("model_name", "gpt-4o"),
        temperature=config.get("temperature", 0.0),
    )
    return llm.bind_tools(self.tools)
```

**Используют этот класс:**
- `product_owner.py` - классификация интентов, координация
- `brainstorm.py` - сбор требований
- `zavhoz.py` - управление ресурсами
- `architect.py` - создание структуры проекта

#### 2. **Developer Node** (`services/langgraph/src/nodes/developer.py`)
Имеет **хардкод** модели на уровне модуля:
```python
llm = ChatOpenAI(model="gpt-4o", temperature=0)
```
⚠️ Этот узел еще **не мигрирован** на `BaseAgentNode`!

#### 3. **База данных** (`services/api/src/models/agent_config.py`)
```python
class AgentConfig(Base):
    model_name: Mapped[str] = mapped_column(String(100), default="gpt-4o", nullable=False)
    temperature: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
```

Сейчас **только имя модели**, без информации о провайдере.

#### 4. **Seed данные** (`scripts/seed_agent_configs.py`)
Все агенты инициализируются с `"model_name": "gpt-4o"`.

---

## 🎯 Что нужно для миграции на OpenRouter

### 1. OpenRouter совместим с LangChain! 

Отличная новость: **OpenRouter предоставляет OpenAI-совместимый API**. Можно продолжать использовать `langchain-openai`.

Пример из документации:
```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ["OPEN_ROUTER_KEY"],
    model="anthropic/claude-3.5-sonnet",  # Любая модель любого провайдера!
    default_headers={
        "HTTP-Referer": "https://your-site.url",
        "X-Title": "Codegen Orchestrator"
    }
)
```

### 2. Доступные модели

OpenRouter предоставляет API для получения списка моделей:
```
GET https://openrouter.ai/api/v1/models
```

Возвращает JSON с метаданными:
- `id` - идентификатор модели (например, `"openai/gpt-4o"`, `"anthropic/claude-3.5-sonnet"`)
- `name` - человекочитаемое имя
- `context_length` - размер контекста
- `pricing` - стоимость
- `architecture.modality` - поддерживаемые модальности (text, image, etc.)

### 3. Формат идентификаторов моделей

OpenRouter использует формат: `{provider}/{model-name}`

Примеры:
- `openai/gpt-4o`
- `openai/gpt-4o-mini`
- `anthropic/claude-3.5-sonnet`
- `google/gemini-2.0-flash-exp`
- `meta-llama/llama-3.1-70b-instruct`
- `mistralai/mistral-large`

---

## 🔧 Предлагаемая архитектура

### Схема базы данных (расширение AgentConfig)

```python
class AgentConfig(Base):
    # ... существующие поля ...
    
    # НОВЫЕ ПОЛЯ:
    llm_provider: Mapped[str] = mapped_column(
        String(50), 
        default="openrouter",  # openrouter | openai | anthropic
        nullable=False
    )
    
    # Для OpenRouter: полный ID типа "openai/gpt-4o"
    # Для прямых провайдеров: краткое имя типа "gpt-4o"
    model_identifier: Mapped[str] = mapped_column(
        String(200),
        default="openai/gpt-4o",
        nullable=False
    )
    
    # Опциональные настройки для OpenRouter
    openrouter_site_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    openrouter_app_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
```

**Backward compatibility**: Поле `model_name` оставляем для отображения в админке.

### Конфигурация LLM клиента

Создать фабрику для инициализации LLM:

```python
# services/langgraph/src/llm/factory.py

class LLMFactory:
    @staticmethod
    def create_llm(config: dict) -> ChatOpenAI:
        provider = config.get("llm_provider", "openrouter")
        model_id = config.get("model_identifier", "openai/gpt-4o")
        temperature = config.get("temperature", 0.0)
        
        if provider == "openrouter":
            return ChatOpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=os.environ["OPEN_ROUTER_KEY"],
                model=model_id,
                temperature=temperature,
                default_headers={
                    "HTTP-Referer": config.get("openrouter_site_url", ""),
                    "X-Title": config.get("openrouter_app_name", "Codegen Orchestrator"),
                }
            )
        elif provider == "openai":
            # Прямое подключение к OpenAI (для обратной совместимости)
            return ChatOpenAI(
                api_key=os.environ.get("OPENAI_API_KEY"),
                model=model_id,
                temperature=temperature,
            )
        # ... другие провайдеры при необходимости
```

### Обновить BaseAgentNode

```python
async def get_llm_with_tools(self):
    config = await self.get_config()
    llm = LLMFactory.create_llm(config)  # ← Использовать фабрику
    return llm.bind_tools(self.tools)
```

---

## 📋 План миграции

### Phase 1: Расширение схемы БД ✅ Минимальный риск

1. **Добавить миграцию Alembic**:
   - Новые поля в `agent_config`: `llm_provider`, `model_identifier`, `openrouter_site_url`, `openrouter_app_name`
   - Default значения для обратной совместимости

2. **Обновить Pydantic схемы** (`services/api/src/schemas/agent_config.py`)

3. **Обновить seed скрипт**:
   ```python
   {
       "id": "product_owner",
       "name": "Product Owner",
       "llm_provider": "openrouter",
       "model_identifier": "openai/gpt-4o",  # Можно выбрать любую модель!
       "model_name": "GPT-4o (OpenRouter)",  # Для отображения
       "temperature": 0.2,
       # ...
   }
   ```

### Phase 2: Создание LLM фабрики ⚡ Низкий риск

1. **Создать `services/langgraph/src/llm/factory.py`**
2. **Добавить тесты** для разных провайдеров
3. **Обновить `BaseAgentNode`** для использования фабрики

### Phase 3: Миграция узлов 🔄 Средний риск

1. **Обновить `developer.py`**: убрать хардкод, мигрировать на `BaseAgentNode`
2. **Проверить все узлы** на использование динамической конфигурации

### Phase 4: API для выбора моделей 🎨 Низкий риск

1. **Создать endpoint** `GET /api/available-models`:
   - Кэшировать список моделей от OpenRouter
   - Фильтровать по модальности, цене, контексту
   
2. **Endpoint для обновления конфигурации агента**:
   - Валидация `model_identifier` против списка доступных моделей
   - Проверка совместимости (например, если агент использует vision, модель должна поддерживать images)

### Phase 5: Админка (позже) 🖥️

1. Dropdown с доступными моделями
2. Фильтры по провайдеру, цене
3. Предпросмотр стоимости на основе контекста

---

## 🧪 Тестирование миграции

### Проверить работу с существующим ключом

```python
# Простой тест
from langchain_openai import ChatOpenAI
import os

llm = ChatOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ["OPEN_ROUTER_KEY"],
    model="openai/gpt-4o",
)

response = llm.invoke("Say hello in Russian")
print(response.content)
```

### Проверить разные модели

```python
# Anthropic через OpenRouter
llm_claude = ChatOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ["OPEN_ROUTER_KEY"],
    model="anthropic/claude-3.5-sonnet",
)

# Google через OpenRouter
llm_gemini = ChatOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ["OPEN_ROUTER_KEY"],
    model="google/gemini-2.0-flash-exp",
)
```

---

## 💡 Преимущества миграции

1. **Гибкость**: Выбор любой модели для каждого агента
   - Product Owner → Claude (лучше понимает intent)
   - Architect → GPT-4o (структурированный вывод)
   - Developer → Deepseek Coder (дешевле для кода)
   
2. **Экономия**:
   - Можно использовать более дешевые модели для простых задач
   - Gemini Flash для быстрых ответов
   - GPT-4o mini для классификации
   
3. **Отказоустойчивость**:
   - Если один провайдер недоступен, быстро переключиться на другого
   - Fallback механизмы
   
4. **Эксперименты**:
   - A/B тестирование моделей
   - Метрики качества по агентам
   
5. **Единая точка управления**:
   - Один ключ для всех провайдеров
   - Централизованные лимиты и мониторинг

---

## ⚠️ Риски и ограничения

### 1. **Rate Limits**
OpenRouter имеет свои лимиты, зависящие от кредитов на аккаунте.

### 2. **Латентность**
Дополнительный hop через OpenRouter может добавить ~50-100ms.

### 3. **Специфичные фичи провайдеров**
Некоторые фичи могут не поддерживаться через OpenRouter (например, custom fine-tuned модели OpenAI).

### 4. **Стоимость**
OpenRouter берет небольшую комиссию (~10-20%) поверх стоимости провайдера.

---

## 🎬 Следующие шаги

1. **Протестировать OPEN_ROUTER_KEY** - убедиться что он работает
2. **Создать миграцию БД** - добавить новые поля
3. **Реализовать LLMFactory** - централизованная логика
4. **Обновить seed данные** - с новым форматом
5. **Мигрировать узлы** - начать с developer.py
6. **Добавить endpoint для моделей** - список доступных моделей
7. **Админка** - UI для выбора моделей (позже)

---

## 📚 Полезные ссылки

- [OpenRouter Docs](https://openrouter.ai/docs)
- [LangChain Integration](https://openrouter.ai/docs/community/lang-chain)
- [Models API](https://openrouter.ai/api/v1/models)
- [Pricing Calculator](https://openrouter.ai/models)
