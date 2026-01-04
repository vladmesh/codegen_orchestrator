# BMAD-Inspired Architecture Roadmap

**Цель**: Эволюция от простого PO-агента к полноценной multi-agent команде с BMAD-структурой.

**Статус**: Planning

**Дата создания**: 2026-01-04

---

## Vision: От MVP к полноценной команде

### Текущее состояние (MVP):

```
User → Telegram → PO Agent (Claude Code)
                       ↓
                  (делает всё сам)
```

### Целевое состояние (BMAD Full):

```
                       User
                        ↓
                    PO Agent
                        ↓
         ┌──────────────┴──────────────┐
         ↓                             ↓
      Analyst                    Zavhoz (DevOps)
         ↓
    Engineering Lead
         ↓
    ┌────┴────┬─────────┐
    ↓         ↓         ↓
Architect Developer Tester
```

**Принципы BMAD:**
- Специализация ролей (каждый агент - эксперт в своей области)
- Структурированные workflow (Analysis → Planning → Solutioning → Implementation)
- Scale-adaptive intelligence (простые задачи = минимум агентов, сложные = полная команда)
- Agile процессы (итерации, ретроспективы, инкременты)

---

## Phase 1: Analysis Layer (3-4 недели)

**Цель**: Добавить Analyst ноду между PO и разработкой для детализации требований.

### Зачем это нужно:

**Проблема:**
- PO даёт бизнес-требования высокого уровня: "Нужна система авторизации"
- Developer не знает деталей: OAuth? JWT? Какие роли? Какие endpoints?
- PO перегружен техническими вопросами
- Developer тратит время на исследование вместо кодинга

**Решение:**
- Analyst принимает feature request от PO
- Исследует: best practices, аналоги, технологии
- Создаёт детальную спецификацию
- Отвечает на технические вопросы от Engineering команды

### Реализация:

#### 1.1 Создать Analyst Node

**Файл**: `services/langgraph/src/nodes/analyst.py`

```python
from langgraph.prebuilt import LLMNode
from shared.schemas import OrchestratorState

class AnalystNode(LLMNode):
    """Research and specification agent.

    Responsibilities:
    - Research solutions and technologies
    - Create detailed specifications
    - Answer engineering team questions
    - Escalate to PO when needed
    """

    role = "analyst"

    tools = [
        "web_search",          # Поиск информации
        "documentation_lookup", # Поиск в документации
        "spec_create",         # Создание спецификации
        "answer",              # Ответ команде
        "escalate_to_po",      # Эскалация к PO
    ]

    system_prompt = """You are a Technical Analyst.

    Your job:
    1. Take high-level feature requests from Product Owner
    2. Research best practices, technologies, frameworks
    3. Create detailed technical specifications
    4. Answer questions from Engineering team
    5. Escalate business questions to PO

    You have access to:
    - Web search for research
    - Documentation lookup
    - Specification templates

    When creating specs, include:
    - Technical approach
    - Technologies/frameworks to use
    - API contracts
    - Data models
    - Security considerations
    - Testing requirements
    """
```

#### 1.2 Добавить Analyst tools

**Файл**: `services/langgraph/src/tools/analyst_tools.py`

```python
@tool
def web_search(query: str) -> str:
    """Search web for information.

    Use for researching:
    - Best practices
    - Technology comparisons
    - Implementation examples
    """
    # Integration с search API
    pass

@tool
def spec_create(
    feature_name: str,
    requirements: str,
    technical_approach: str,
) -> str:
    """Create technical specification document.

    Saves spec to project repository.
    Returns spec ID and location.
    """
    # Создаёт markdown spec в repo
    pass

@tool
def escalate_to_po(question: str) -> str:
    """Escalate question to Product Owner.

    Use when:
    - Business decision needed
    - Requirements unclear
    - Priority clarification needed
    """
    # Отправляет вопрос PO через orchestrator answer
    pass
```

#### 1.3 Обновить граф

**Файл**: `services/langgraph/src/graph.py`

```python
# Добавляем Analyst между PO и Engineering
graph.add_node("po", PONode())
graph.add_node("analyst", AnalystNode())  # NEW
graph.add_node("engineering", engineering_subgraph)

# Routing logic
def route_from_po(state: OrchestratorState) -> str:
    """Route PO decisions."""
    intent = state.get("intent")

    if intent == "feature_request":
        return "analyst"  # Analyst исследует и детализирует
    elif intent == "infrastructure":
        return "zavhoz"
    else:
        return END

graph.add_conditional_edges("po", route_from_po)
graph.add_edge("analyst", "engineering")
```

#### 1.4 Agent-to-agent questions

**Новый механизм**: Engineering агент может задать вопрос Analyst

```python
# В Developer агенте
orchestrator ask --to=analyst "Какую библиотеку для валидации использовать?"

# Workers-spawner роутит вопрос
# Analyst отвечает через stdin Developer агента
```

**Реализация routing:**

```python
# services/workers-spawner/src/workers_spawner/tool_executor.py

async def _handle_ask(self, args: dict) -> dict:
    """Handle 'orchestrator ask' tool.

    Routes question to specified agent.
    """
    message = args.get("message")
    to = args.get("to")  # agent role: "analyst", "po", "engineering_lead"
    from_agent = args.get("from_agent_id")

    # Find target agent by role
    target_agent_id = await self._find_agent_by_role(to)

    if not target_agent_id:
        return {
            "success": False,
            "error": f"Agent with role '{to}' not found",
        }

    # Format question with context
    question_text = f"[QUESTION from {from_agent}]\n{message}\n"

    # Write to target agent's stdin
    await self.process_manager.write_to_stdin(target_agent_id, question_text)

    # Setup response listener
    # TODO: How to get answer back?

    return {
        "success": True,
        "message": "Question sent, waiting for response...",
    }
```

### Success Criteria Phase 1:

- ✅ Analyst принимает feature request от PO
- ✅ Analyst создаёт детальную спецификацию
- ✅ Engineering агент может задать вопрос Analyst
- ✅ Analyst может эскалировать к PO
- ✅ Спецификации хранятся в repo

---

## Phase 2: Engineering Leadership (4-5 недель)

**Цель**: Добавить Engineering Lead для координации разработки.

### Зачем это нужно:

**Проблема:**
- Developer, Architect, Tester работают независимо
- Нет координации: кто что делает?
- Конфликты в коде
- Architect принял решение, но Developer не знает
- Tester не знает что тестировать

**Решение:**
- Engineering Lead координирует команду
- Назначает задачи
- Проводит code review
- Отвечает на архитектурные вопросы
- Эскалирует к Analyst если нужно

### Реализация:

#### 2.1 Создать Engineering Lead Node

**Файл**: `services/langgraph/src/nodes/engineering_lead.py`

```python
class EngineeringLeadNode(LLMNode):
    """Engineering team coordinator.

    Responsibilities:
    - Receive spec from Analyst
    - Coordinate Architect → Developer → Tester workflow
    - Review code
    - Answer technical questions
    - Escalate to Analyst when needed
    """

    role = "engineering_lead"

    tools = [
        "assign_task",      # Назначить задачу агенту
        "review_code",      # Code review
        "approve_merge",    # Approve PR
        "answer",           # Ответить команде
        "escalate_to_analyst",
    ]

    system_prompt = """You are an Engineering Lead.

    Your job:
    1. Receive technical specifications from Analyst
    2. Coordinate the engineering workflow:
       - Architect designs system
       - Developer implements code
       - Tester verifies quality
    3. Conduct code reviews
    4. Answer technical questions from team
    5. Escalate to Analyst if spec unclear

    Workflow:
    1. Review spec from Analyst
    2. Assign architecture task to Architect
    3. Wait for design
    4. Assign implementation to Developer
    5. Review code
    6. Assign testing to Tester
    7. Approve and merge
    """
```

#### 2.2 Subgraph coordination

**Текущий Engineering субграф** работает линейно:

```python
# Сейчас:
Architect → Developer → Tester
```

**С Lead** появляется координация:

```python
# После Phase 2:
Engineering Lead (координатор)
    ↓ assign task
Architect (думает)
    ↓ design ready
Engineering Lead (review)
    ↓ assign implementation
Developer (кодит)
    ↓ code ready
Engineering Lead (code review)
    ↓ assign testing
Tester (тестирует)
    ↓ tests pass
Engineering Lead (approve merge)
```

**Реализация:**

```python
# services/langgraph/src/subgraphs/engineering.py

from langgraph.graph import StateGraph

def create_engineering_subgraph():
    """Engineering subgraph with Lead coordination."""

    subgraph = StateGraph(EngineeringState)

    # Nodes
    subgraph.add_node("lead", EngineeringLeadNode())
    subgraph.add_node("architect", ArchitectNode())
    subgraph.add_node("developer", DeveloperNode())
    subgraph.add_node("tester", TesterNode())

    # Entry point
    subgraph.set_entry_point("lead")

    # Routing from Lead
    def route_from_lead(state):
        action = state.get("lead_action")

        if action == "architecture_needed":
            return "architect"
        elif action == "implementation_needed":
            return "developer"
        elif action == "testing_needed":
            return "tester"
        else:
            return END

    subgraph.add_conditional_edges("lead", route_from_lead)

    # Return to Lead after each step
    subgraph.add_edge("architect", "lead")
    subgraph.add_edge("developer", "lead")
    subgraph.add_edge("tester", "lead")

    return subgraph.compile()
```

### Success Criteria Phase 2:

- ✅ Engineering Lead координирует Architect → Developer → Tester
- ✅ Code review автоматизирован
- ✅ Команда может задавать вопросы Lead
- ✅ Lead эскалирует к Analyst при неясностях

---

## Phase 3: Scrum Master & Agile Processes (3-4 недели)

**Цель**: Добавить Scrum Master для процессного управления.

### Зачем это нужно:

**Проблема:**
- Нет планирования спринтов
- Задачи блокируются - никто не замечает
- Нет ретроспектив и улучшений
- Команда не знает текущий прогресс

**Решение:**
- Scrum Master управляет процессами
- Проводит sprint planning
- Отслеживает blockers
- Facilitates ретроспективы
- Собирает метрики

### Реализация:

#### 3.1 Создать Scrum Master Node

**Файл**: `services/langgraph/src/nodes/scrum_master.py`

```python
class ScrumMasterNode(LLMNode):
    """Agile process facilitator.

    Responsibilities:
    - Sprint planning
    - Daily standups
    - Remove blockers
    - Sprint retrospectives
    - Track metrics
    """

    role = "scrum_master"

    tools = [
        "create_sprint",
        "add_to_backlog",
        "assign_story_points",
        "track_velocity",
        "identify_blockers",
        "facilitate_retro",
    ]

    system_prompt = """You are a Scrum Master.

    Your job:
    1. Plan sprints with PO and team
    2. Facilitate daily standups
    3. Identify and remove blockers
    4. Track team velocity
    5. Conduct retrospectives

    You work with:
    - PO: Prioritize backlog
    - Engineering Lead: Capacity planning
    - All team: Daily standups

    Key metrics:
    - Sprint velocity
    - Blocker resolution time
    - Sprint completion rate
    """
```

#### 3.2 Sprint workflow

**Sprint planning:**

```
Week 0 Monday:
1. Scrum Master: создаёт sprint
2. PO: приоритизирует backlog
3. Engineering Lead: оценивает capacity
4. Team: story point оценки
5. Scrum Master: finalizes sprint plan

Week 0-2:
- Daily standups (async via messages)
- Blocker tracking
- Progress updates

Week 2 Friday:
- Sprint review
- Retrospective
- Metrics collection
```

#### 3.3 Blocker detection

**Автоматический детект blockers:**

```python
# Scrum Master tool
@tool
def identify_blockers() -> list[dict]:
    """Identify current blockers.

    Checks:
    - Agents waiting >2 hours for response
    - Failed tool calls
    - Test failures
    - Deployment issues
    """
    blockers = []

    # Check agent wait times
    for agent in active_agents:
        if agent.last_activity > 2_hours_ago:
            if agent.waiting_for_response:
                blockers.append({
                    "agent": agent.id,
                    "type": "waiting_for_response",
                    "duration": agent.wait_duration,
                })

    # Check failed tool calls
    failed_tools = get_failed_tool_calls(last_24h)
    for fail in failed_tools:
        blockers.append({
            "agent": fail.agent_id,
            "type": "tool_failure",
            "tool": fail.tool_name,
            "error": fail.error,
        })

    return blockers
```

### Success Criteria Phase 3:

- ✅ Sprint planning работает
- ✅ Blockers детектятся автоматически
- ✅ Velocity tracked
- ✅ Retrospectives documented

---

## Phase 4: Scale-Adaptive Intelligence (5-6 недель)

**Цель**: Автоматически адаптировать количество агентов под сложность задачи.

### Зачем это нужно:

**Проблема:**
- Простая задача "fix typo" не требует всей команды
- Сложная задача "build e-commerce platform" требует всех

**Решение:**
- PO классифицирует задачу: simple | medium | complex
- Граф автоматически выбирает workflow:
  - **Simple**: PO → Developer → Done
  - **Medium**: PO → Analyst → Developer → Tester
  - **Complex**: Full BMAD team

### Реализация:

#### 4.1 Task complexity classifier

**Файл**: `services/langgraph/src/classifiers/complexity.py`

```python
from enum import Enum

class TaskComplexity(Enum):
    SIMPLE = "simple"      # 1-2 hours, 1-2 agents
    MEDIUM = "medium"      # 1-3 days, 3-5 agents
    COMPLEX = "complex"    # 1+ weeks, full team
    EPIC = "epic"          # Months, multiple teams

def classify_task(description: str) -> TaskComplexity:
    """Classify task complexity using LLM.

    Factors:
    - Scope of work
    - Number of components affected
    - Technical uncertainty
    - Dependencies
    """

    prompt = f"""Classify this task complexity:

    Task: {description}

    Consider:
    - Scope (lines of code, files affected)
    - Uncertainty (known vs unknown)
    - Dependencies (isolated vs interconnected)

    Return one of: simple, medium, complex, epic
    """

    # LLM call
    result = llm.predict(prompt)
    return TaskComplexity(result.lower())
```

#### 4.2 Adaptive routing

**Файл**: `services/langgraph/src/graph.py`

```python
def route_by_complexity(state: OrchestratorState) -> str:
    """Route based on task complexity."""

    task = state.get("current_task")
    complexity = classify_task(task.description)

    if complexity == TaskComplexity.SIMPLE:
        # Skip analysis, go straight to developer
        return "developer"

    elif complexity == TaskComplexity.MEDIUM:
        # Analyst → Engineering (skip Lead)
        return "analyst"

    elif complexity == TaskComplexity.COMPLEX:
        # Full workflow
        return "analyst"  # → Engineering Lead → Full team

    elif complexity == TaskComplexity.EPIC:
        # Multiple sprints, Scrum Master involved
        return "scrum_master"

graph.add_conditional_edges("po", route_by_complexity)
```

### Success Criteria Phase 4:

- ✅ Simple tasks skip unnecessary agents
- ✅ Complex tasks use full team
- ✅ Classification accuracy >80%

---

## Phase 5: Multi-Team Coordination (6-8 недель)

**Цель**: Несколько Engineering команд работают параллельно.

### Зачем это нужно:

**Проблема:**
- Большой проект: frontend + backend + mobile + devops
- Одна команда не справляется
- Зависимости между компонентами

**Решение:**
- Несколько Engineering субграфов
- Tech Lead координирует между командами
- Shared backlog management

### Реализация:

#### 5.1 Multi-team graph

```python
# Архитектура:
PO
 ↓
Analyst
 ↓
Tech Lead (NEW - координирует команды)
 ↓
┌──────────┬──────────┬──────────┐
↓          ↓          ↓          ↓
Frontend   Backend   Mobile    DevOps
Team       Team      Team      Team

# Каждая team = отдельный Engineering субграф
```

#### 5.2 Cross-team communication

**Проблема**: Frontend team нужен API endpoint от Backend team

**Решение**:

```python
# Frontend Developer
orchestrator ask --to=backend_team "Когда готов /api/users endpoint?"

# Backend Lead отвечает
orchestrator answer --to=frontend_team "Endpoint готов, см. /api/docs"
```

#### 5.3 Dependency tracking

**Tool**: `orchestrator depends-on`

```python
# Frontend task
orchestrator depends-on --task=backend_task_123

# Scrum Master tracking
dependencies = get_cross_team_dependencies()
# Alerts if dependency blocked
```

### Success Criteria Phase 5:

- ✅ 4 команды работают параллельно
- ✅ Cross-team communication работает
- ✅ Dependencies tracked

---

## Phase 6: Advanced Capabilities (Ongoing)

### 6.1 Context Compaction

**Проблема**: После 2 часов работы контекст агента = 200k tokens

**Решение**:

```python
# Auto-compact при достижении лимита
if context_tokens > 150_000:
    orchestrator compact-context
    # Сохраняет ключевые решения, удаляет детали
```

### 6.2 Streaming Responses

**Проблема**: Юзер ждёт 30 секунд пока агент думает

**Решение**: SSE stream частичных ответов

```python
# Agent пишет в stdout постепенно
"Analyzing requirements..."
"Found 3 similar implementations..."
"Choosing approach B because..."
"Final answer: Use OAuth 2.0"

# Telegram bot показывает в реалтайме
```

### 6.3 Long-term Memory

**Проблема**: Агент не помнит прошлые проекты

**Решение**: Vector DB для history

```python
# При новой задаче
similar_tasks = vector_db.search(task_description)
# Подсказка агенту из прошлого опыта
```

### 6.4 Human-in-the-loop

**Проблема**: Критичное решение - нужно одобрение

**Решение**:

```python
# Agent
orchestrator request-approval --description="Delete production DB" --severity=critical

# Telegram бот просит подтверждения у админа
# После одобрения - продолжает
```

---

## Implementation Priority

| Phase | Priority | Value | Effort | ROI |
|-------|----------|-------|--------|-----|
| 1: Analyst | High | High | Medium | 🔥 High |
| 2: Engineering Lead | High | High | High | 🔥 High |
| 3: Scrum Master | Medium | Medium | Medium | Medium |
| 4: Adaptive | Medium | High | Medium | High |
| 5: Multi-team | Low | High | Very High | Medium |
| 6: Advanced | Low | Medium | Variable | Variable |

**Recommendation**: Фазы 1 и 2 критичны, делать первыми.

---

## Metrics & Success Tracking

### Team Performance:

- **Velocity**: Story points per sprint
- **Quality**: Bug rate, test coverage
- **Efficiency**: Time from spec to deployment
- **Collaboration**: Questions asked/answered

### Agent Performance:

- **Response time**: How fast agents answer
- **Tool success rate**: % successful tool calls
- **Context retention**: How much history needed
- **Escalation rate**: How often escalate to human

### Business Metrics:

- **Time to market**: Feature request → deployment
- **Cost per feature**: Agent compute costs
- **User satisfaction**: Telegram bot feedback
- **Reliability**: Uptime, error rates

---

## Technology Stack Evolution

### Current (MVP):

- LangGraph (orchestration)
- Claude Code (agents)
- Redis (messaging, logs)
- PostgreSQL (metadata)
- Docker (isolation)

### Future additions:

- **Vector DB** (Pinecone/Weaviate) - long-term memory
- **Prometheus/Grafana** - metrics & monitoring
- **S3/MinIO** - log archival
- **RabbitMQ** - complex message routing?
- **Kubernetes** - multi-team scaling

---

## Open Questions

1. **Agent cost optimization**
   - Как минимизировать LLM API costs?
   - Когда использовать cheaper models (Haiku)?
   - Caching strategies?

2. **Conflict resolution**
   - Что если Architect и Developer не согласны?
   - Кто принимает финальное решение?
   - Нужен ли voting механизм?

3. **Quality gates**
   - Автоматические code quality checks?
   - Security scanning?
   - Performance benchmarks?

4. **Rollback procedures**
   - Что если deployment failed?
   - Automated rollback?
   - Post-mortem analysis?

---

## Research Areas

### 1. Agent Communication Protocols

**Current**: Simple text messages в stdin/stdout

**Research**: Structured protocols
- JSON-RPC
- GraphQL subscriptions
- gRPC streams

### 2. Multi-Agent Consensus

**Problem**: Disagreement между агентами

**Research**:
- Voting algorithms
- Consensus protocols (Raft, Paxos)
- Weighted opinions by expertise

### 3. Adaptive Learning

**Problem**: Agents не учатся на опыте

**Research**:
- Reinforcement learning for workflow
- Meta-learning для task routing
- Continuous improvement loops

---

## Conclusion

Эволюция к BMAD-структуре - это journey, не destination.

**Ключевые принципы:**

1. **Iterative**: Добавляем агентов постепенно
2. **Data-driven**: Метрики определяют приоритеты
3. **User-focused**: Value для пользователя > красота архитектуры
4. **Pragmatic**: Простое решение > сложное, если работает

**Roadmap timeline:**

- **Q1 2026**: MVP + Phase 1 (Analyst)
- **Q2 2026**: Phase 2 (Engineering Lead) + Phase 3 (Scrum Master)
- **Q3 2026**: Phase 4 (Adaptive) + optimization
- **Q4 2026**: Phase 5 (Multi-team) exploration

**Success = **команда агентов, которая autonomously доставляет value как настоящая agile team.

---

**Документ обновлён**: 2026-01-04
**Автор**: Claude Sonnet 4.5
**Статус**: Vision Document
