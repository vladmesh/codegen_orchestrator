"""The exact values a confirmed brief starts its product with, for seed and for QA alike.

The brief's own settings, then the answers its stored capability plan maps to exact
product keys (`CapabilityPlan.settings`). Nothing is derived from prose or reinterpreted:
the deploy seed writes exactly these, and QA reads exactly these back.
"""

from shared.contracts.dto.product_brief import InitialSetting, ProductBriefRead, SettingScope


async def confirmed_product_settings(brief: ProductBriefRead, api) -> list[InitialSetting]:
    """`api` is the caller's orchestrator API client, which reads the stored plan."""
    settings = list(brief.content.initial_settings)
    if brief.content.capabilities is None:
        return settings
    plan = await api.get_capability_plan(brief.id)
    if plan is None:
        raise RuntimeError(f"Product Brief {brief.id} names capabilities but has no plan")
    return settings + [
        InitialSetting(key=item.key, scope=SettingScope.PRODUCT, value=item.value)
        for item in plan.settings
    ]
