# AGENTS Hard Rules for QuantAda

## 0) Scope
This file defines non-negotiable engineering rules for all coding agents working in this repo.
If a proposed change conflicts with these rules, reject or redesign it.

## 0.5) Documentation Hierarchy
1. `docs/specs/*` is the formalized repository spec layer for agent-facing development.
2. `agent_prompts/*` is the code-generation template layer; it is not the primary contract source.
3. Source code + tests remain the final reality check.
4. If docs/specs, prompts, and code diverge:
- align behavior to current code/tests first
- then update `docs/specs/*` and `agent_prompts/*` in the same change

## 1) Core Principles (Non-Negotiable)
1. High Self-Healing First
- Prefer recovery over perfection: reconnect, retry, reconcile, alarm.
- Runtime failures must degrade safely and continue when possible.

2. Stateless First
- Broker reality is source of truth.
- Live correctness must come from broker reality plus short-lived reconciliation/health flags only; do not persist trade intent as state.
- Do NOT introduce cross-K intent memory queues for buy retry or deferred execution.
- Do NOT reintroduce old deferred/buffered mechanisms.

3. Minimal Change First
- Implement the smallest effective fix.
- Avoid adding new switches/knobs unless required by clear operational need.
- Prefer local, targeted edits over broad refactors.
- Configuration boundary: do not add module-local, broker-specific, one-off, or
  compatibility settings to `config.py` merely to make them CLI-configurable.
  Prefer safe defaults owned by the responsible module; if runtime override is
  genuinely required, register it in the explicit `run.py --config` allowlist.
  Only stable, cross-module public settings with a clear operational need belong
  in `config.py`; new settings must update the specs, allowlist, and tests together.
  Local exceptions are acceptable for personal or small-scope use when their
  scope and rationale are documented; do not add abstractions for theoretical
  uniformity without practical benefit.

4. File Responsibility / Cohesion First
- Keep each file centered on its primary runtime responsibility.
- Large orchestration modules must not absorb cross-cutting utilities such as terminal tee, log path persistence, command formatting, or file IO helpers when a focused module can own them.
- Optimizer code should own search orchestration, objective evaluation, study lifecycle, and training/reporting flow; runtime logging utilities belong in dedicated modules.
- Base classes should expose contracts and stable public entrypoints only; implementation mechanics such as caches, terminal tee, command construction, or report formatting belong in focused common/runtime modules.
- If a base class needs a thin public method for strategy/broker authors, keep the method as an API adapter and move the mechanical implementation behind that API into the focused module.
- When a change adds non-core behavior to an already broad file, first look for a small focused extraction instead of adding more incidental logic.

5. Execution Discipline
- Keep behavior deterministic and auditable.
- Follow existing execution semantics consistently (sellability guard, immediate downgrade retry, daily cleanup policy).
- Live-only execution guards such as pending-order waits, rolling buys, cash settlement waits, broker sync, and realtime pending-order queries must not run in backtests. Backtests must assume planned orders execute synchronously and remain fast.
- Any new execution-path logic must preserve this split at the call boundary: live may poll, reconcile, or wait only within a bounded current-run scope; backtests and optimizations must remain in-memory, synchronous, and non-blocking.

6. Anti-Abstraction Discipline
- Do not introduce a new helper method, wrapper, mixin, bridge, or base-class API unless it clearly pays rent now.
- If logic has only one call site, prefer keeping it local unless extraction materially improves correctness, testability, or readability.
- Do not add thin pass-through wrappers that merely rename or forward a single call without reducing real complexity.
- Do not move strategy-specific or broker-specific behavior into `base_*` classes unless at least two concrete implementations already need the same stable contract.
- Prefer deleting obsolete compatibility layers over keeping two ways to do the same thing.
- When touching a bloated base class, prefer shrinking or localizing logic instead of adding one more abstraction on top.

## 2) Architecture Contracts (Must Follow)
1. Respect base interfaces and contracts:
- `live_trader/adapters/base_broker.py`
- `strategies/base_strategy.py`
- `stock_selectors/base_selector.py`
- `risk_controls/base_risk_control.py`
- When touching related module types, also respect:
  - `data_providers/base_provider.py`
  - `alarms/base_alarm.py`
  - `recorders/base_recorder.py`

2. Live adapter module contract:
- Each `live_trader/adapters/*_broker.py` loaded by `LiveTrader` must expose both:
  - a `BaseLiveBroker` subclass
  - a `BaseDataProvider` subclass discoverable in the same module
- Order proxy runtime contract must satisfy not only `BaseOrderProxy` abstract methods, but also current engine expectations such as `status`, `executed`, `data`, and `is_accepted()`.

3. Strategy-side execution contract:
- Current equal-weight rebalance API is `BaseStrategy.execute_rebalance(target_symbols, top_k, rebalance_threshold)`.
- `target_symbols` should be a list of data objects, not weight dicts or raw symbol strings.
- Prefer iterating `self.broker.datas` directly in trading loops unless a strategy explicitly needs a narrower local list.

4. Broker-side hard constraints:
- Pending orders contract includes `id` in `get_pending_orders`.
- Implement `cancel_pending_order(order_id)` with safe failure behavior (False instead of crash).
- No local fake cash/position as long-lived source of truth.
- If a live adapter cannot trust the current pending-order snapshot, it must expose that as a transient health flag rather than returning a silent empty truth. Backtest adapters must not rely on live pending-order state.

5. Order-state semantics:
- Rejected BUY: immediate same-bar downgrade retry path is preferred.
- Multi-symbol retries must be independent.
- A-share/T+1 markets must use sellable semantics, not total position only.

6. Live self-healing baseline:
- Do not regress multi-risk chaining, live-refresh completeness gate, empty-data recovery, stale `strategy.order` auto-clear, or schedule prewarm paths without explicit failure evidence and tests.

7. Overnight pending order policy:
- Live run performs overnight cleanup before refresh unless `KEEP_OVERNIGHT_ORDERS=True`.
- Cleanup may retry; failures must be logged and alarmed.

## 3) Forbidden Patterns
- Reintroducing `_deferred_orders`, `_buffered_rejected_retries`, or similar queue replay design.
- Persisting stale intent to force next-day replay of prior-day buy decisions.
- Expanding state machines without explicit failure evidence and tests.
- Do not expand `config.py` for a single caller, local default, temporary
  workaround, or compatibility alias; when a runtime override is genuinely
  necessary, use a safe default in the responsible module and register an
  explicit CLI override.
- Adding base-class methods, config knobs, or compatibility shims for a single current caller or one-off scenario.
- Extracting one-use local logic into named helpers or wrapper classes without a concrete second use case.

## 4) Fast-Generation Workflow (Mandatory)
When user asks for rapid code generation or new module scaffolding, agents must follow this order:
1. Read relevant `docs/specs/*` first.
2. Then read relevant `agent_prompts/*` (broker/strategy/debug_fix/etc.).
3. Then read corresponding base class interface(s) and loader/runtime contracts.
4. Generate code strictly against spec + prompt + base/runtime contracts.
5. If spec/prompt and code diverge, align to current code/tests, then update `docs/specs/*` and `agent_prompts/*` in same change.

## 5) Testing and Verification Discipline
1. Every behavioral change must include focused tests or updated assertions.
2. Changes touching execution, pending orders, cash/position reconciliation, or scheduling must include both live-path assertions and backtest/optimization fast-path assertions when feasible.
3. Always run targeted tests first, then run broader regression when feasible.
4. Report what was validated and what was not validated.

## 6) Communication Style for Agents
- Be concise, direct, and pragmatic.
- Prioritize actionable outcomes over long theory.
- Challenge complexity creep politely; default to simpler robust design.

## 7) Decision Ownership
- AI can propose and rank with high weight.
- Final GO/HOLD/KILL decisions remain human-owned.
