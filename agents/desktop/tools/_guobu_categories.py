"""国补品类归一 —— 别名表**不在这里**, 在 ``sources/category-enum.yaml``。

本模块现在只剩一层的语义: 「在资料卡登记的那些品类里归一」。

- **别名数据不在这里**: 全表的唯一数据源是 ``sources/category-enum.yaml``,
  经 ``_category_enum.match_among`` 读入。本模块原先那份硬编码 ``ALIASES`` (10 个 id)
  已删 —— 留着就是第二份数据: 运行期不读它, 判据却在盯着它, 两边可以各说各话而没人发现。
- **品类清单也不在这里**, 在资料卡 ``fact-cards/guobu-2026.yaml`` 的 ``categories``,
  由 ``_fact_cards.category_names()`` 作为**候选集**传进来。
- **档位归属(家电/数码)也不在这里**, 在资料卡的 ``tier``, 由
  ``_fact_cards.params_of()`` 合并后交给调用方。

版本:
- v2(2026-09-01, 枚举化改造): 品类归一化交给模型, 工具侧只做确定性兜底;
- v1.3: 删组合词穷举(PARTS_WORDS) —— 电视柜/空调扇/手机壳由「模型映射不出枚举
  → 不调工具」兜底, 工具侧的收尾匹配天然不命中(它们不以品类词**结尾**);
- v1.4: 清单与档位移交资料卡, 本模块只留别名。
- v1.5(本次): 别名也移出代码, 改由 ``_category_enum`` 从枚举读。本模块退成一层适配,
  行为**逐字不变** —— 由 ``test_category_enum.py`` 里那份切换前录下的逐词结果钉住。
"""

from collections.abc import Iterable

import _category_enum


def match_category(subject: str, categories: Iterable[str]) -> str | None:
    """返回归一品类名; 无法确定返回 ``None``。

    *categories* 是资料卡登记的品类名集合(``_fact_cards.category_names()``), 同时也是
    **候选集**: 返回值一定落在它里面, 落不进去就是 ``None``。归一用的别名来自
    ``sources/category-enum.yaml``, 规则是**完全相等或以别名结尾, 取最长命中** ——
    这样「平板电脑」落到平板而不是电脑(前提是资料卡的候选集里两个都有)。

    只做确定性兜底, 不做语义猜测: 电视柜 / 空调扇 / 手机壳这类组合词不以品类词
    结尾, 天然不命中, 由「模型映射不出枚举 → 不调工具」在上一层拦住。

    表丢了会**抛** ``FileNotFoundError``, 不静默回落到「一个品类都不认识」——
    后者的表现是一句平静的「这个品类不在国补范围内」, 而那是错的。
    """
    return _category_enum.match_among(_category_enum.load_enum_sync(), subject, categories)
