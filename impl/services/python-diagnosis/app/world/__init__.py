"""世界模型（docs/43 §10.3，Phase C）。

L1 统计世界模型（本期）：加权线性退化 + 反事实推演，输出必带不确定性区间。
L2 神经世界模型（预留）：接口与本包一致（fit/predict/counterfactual 签名稳定），
需 LLM 预算与训练数据后替换实现。
"""

from . import model  # noqa: F401
from .model import DegradationModel, counterfactual, fit  # noqa: F401
