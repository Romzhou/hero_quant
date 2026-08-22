"""期权定价：Black-Scholes 标量实现，含到期退化与隐含波动率。

职责：提供 bs_price/price、bs_greeks（delta/gamma/vega/theta/rho）、implied_volatility 及其别名。
架构位置：quantlib 定价子集，供上层工具与回测定价钩子调用。
关键设计：T≤0 返回内在价值；sigma≈0 返回贴现内在价值；优先 scipy.stats.norm 否则 math.erf 回落；仅标量 API。
"""

from __future__ import annotations

import math

try:
    from scipy.stats import norm as _scipy_norm  # type: ignore

    def _norm_cdf(x: float) -> float:
        return float(_scipy_norm.cdf(x))

    def _norm_pdf(x: float) -> float:
        return float(_scipy_norm.pdf(x))

except Exception:  # fallback without scipy

    def _norm_cdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    def _norm_pdf(x: float) -> float:
        return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _resolve_bs_args(S, K, T, r, sigma, option_type, kwargs):
    """归一化 BS 参数：兼容大小写与常见别名（s/spot、k/strike、t/tau、vol/volatility、cp/type 等）。"""
    # lowercase aliases via kwargs
    if S is None:
        S = kwargs.get("s", kwargs.get("spot", kwargs.get("underlying")))
    if K is None:
        K = kwargs.get("k", kwargs.get("strike"))
    if T is None:
        T = kwargs.get("t", kwargs.get("tau", kwargs.get("time_to_expiry")))
    if "r" in kwargs:
        r = kwargs["r"]
    if "risk_free" in kwargs:
        r = kwargs["risk_free"]
    if "sigma" in kwargs:
        sigma = kwargs["sigma"]
    if "vol" in kwargs:
        sigma = kwargs["vol"]
    if "volatility" in kwargs:
        sigma = kwargs["volatility"]
    if "option_type" in kwargs:
        option_type = kwargs["option_type"]
    if "type" in kwargs:
        option_type = kwargs["type"]
    if "cp" in kwargs:
        option_type = kwargs["cp"]
    return S, K, T, r, sigma, option_type


def bs_price(S=None, K=None, T=None, r: float = 0.05, sigma: float = 0.2, option_type: str = "call", **kwargs) -> float:
    """Black-Scholes 定价：T≤0 取内在价值，sigma≈0 取贴现内在价值，否则按 d1/d2 公式计算。"""
    S, K, T, r, sigma, option_type = _resolve_bs_args(S, K, T, r, sigma, option_type, kwargs)

    # 必需参数校验
    if S is None or K is None or T is None:
        raise TypeError("bs_price requires S, K, T (e.g. bs_price(S=100,K=100,T=0))")

    try:
        S_f = float(S)
        K_f = float(K)
        T_f = float(T)
        r_f = float(r)
        sigma_f = float(sigma)
    except Exception as e:
        raise TypeError(f"invalid numeric args: {e}")

    is_call = str(option_type).strip().lower().startswith("c")

    # 到期退化：T≤0 或极小值直接取内在价值，避免除零（阈值 1e-12）
    if T_f <= 0 or T_f < 1e-12:
        if is_call:
            return max(S_f - K_f, 0.0)
        else:
            return max(K_f - S_f, 0.0)

    # 零波动退化：贴现内在价值
    if sigma_f <= 0 or sigma_f < 1e-12:
        df = math.exp(-r_f * T_f)
        if is_call:
            return max(S_f - K_f * df, 0.0)
        else:
            return max(K_f * df - S_f, 0.0)

    # 非法现货/行权价保护：回落至贴现内在价值
    if S_f <= 0 or K_f <= 0:
        if is_call:
            return max(S_f - K_f * math.exp(-r_f * T_f), 0.0)
        else:
            return max(K_f * math.exp(-r_f * T_f) - S_f, 0.0)

    sqrt_T = math.sqrt(T_f)
    # d1/d2 标准公式
    try:
        d1 = (math.log(S_f / K_f) + (r_f + 0.5 * sigma_f * sigma_f) * T_f) / (sigma_f * sqrt_T)
        d2 = d1 - sigma_f * sqrt_T
    except (ValueError, ZeroDivisionError):
        # 数值异常回落为内在价值
        if is_call:
            return max(S_f - K_f, 0.0)
        else:
            return max(K_f - S_f, 0.0)

    df = math.exp(-r_f * T_f)
    if is_call:
        price = S_f * _norm_cdf(d1) - K_f * df * _norm_cdf(d2)
    else:
        price = K_f * df * _norm_cdf(-d2) - S_f * _norm_cdf(-d1)

    # 数值误差截断：极小负值归零
    if price < 0 and price > -1e-12:
        price = 0.0
    if price < 0:
        price = max(price, 0.0)  # 价格下界 0
    return float(price)


# 别名：与 bs_price 同义
price = bs_price


def bs_greeks(S=None, K=None, T=None, r: float = 0.05, sigma: float = 0.2, option_type: str = "call", **kwargs) -> dict:
    """Black-Scholes Greeks：到期时 delta 为示性函数，其余希腊字母为 0；否则按解析公式计算。"""
    S, K, T, r, sigma, option_type = _resolve_bs_args(S, K, T, r, sigma, option_type, kwargs)

    if S is None or K is None or T is None:
        raise TypeError("bs_greeks requires S, K, T")

    try:
        S_f = float(S)
        K_f = float(K)
        T_f = float(T)
        r_f = float(r)
        sigma_f = float(sigma)
    except Exception as e:
        raise TypeError(f"invalid numeric args: {e}")

    is_call = str(option_type).strip().lower().startswith("c")

    # 到期退化：delta 为示性，其余希腊字母无时间价值
    if T_f <= 0 or T_f < 1e-12:
        if is_call:
            delta = 1.0 if S_f > K_f else (0.5 if S_f == K_f else 0.0)
        else:
            delta = -1.0 if S_f < K_f else (-0.5 if S_f == K_f else 0.0)
            if S_f == K_f:
                delta = -0.5  # 平值 put 的 convention 取 -0.5
        return {"delta": float(delta), "gamma": 0.0, "vega": 0.0, "theta": 0.0, "rho": 0.0}

    if sigma_f <= 0 or sigma_f < 1e-12 or S_f <= 0 or K_f <= 0:
        return {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0, "rho": 0.0}  # 退化：无波动/非法标的不产生希腊暴露

    sqrt_T = math.sqrt(T_f)
    d1 = (math.log(S_f / K_f) + (r_f + 0.5 * sigma_f * sigma_f) * T_f) / (sigma_f * sqrt_T)
    d2 = d1 - sigma_f * sqrt_T
    nd1 = _norm_cdf(d1)
    nd2 = _norm_cdf(d2)
    pdf_d1 = _norm_pdf(d1)
    df = math.exp(-r_f * T_f)

    if is_call:
        delta = nd1
        rho = K_f * T_f * df * nd2
    else:
        delta = nd1 - 1.0
        rho = -K_f * T_f * df * _norm_cdf(-d2)

    gamma = pdf_d1 / (S_f * sigma_f * sqrt_T)  # 二阶价格敏感
    vega = S_f * pdf_d1 * sqrt_T  # 波动率敏感（每 1 单位 vol）
    # theta 为年化时间衰减
    term1 = -(S_f * pdf_d1 * sigma_f) / (2 * sqrt_T)
    if is_call:
        theta = term1 - r_f * K_f * df * nd2
    else:
        theta = term1 + r_f * K_f * df * _norm_cdf(-d2)

    return {
        "delta": float(delta),
        "gamma": float(gamma),
        "vega": float(vega),
        "theta": float(theta),
        "rho": float(rho),
    }


def implied_volatility(price: float, S=None, K=None, T=None, r: float = 0.05, option_type: str = "call", **kwargs) -> float:
    """隐含波动率：二分法反推，鲁棒无牛顿迭代；价格不高于内在价值或 T≤0 时回落 0。"""
    # 归一化参数别名
    S, K, T, r, _sigma_dummy, option_type = _resolve_bs_args(S, K, T, r, 0.2, option_type, kwargs)

    if S is None or K is None or T is None:
        # try to extract from kwargs alternative names that may have been passed as extra positional?
        raise TypeError("implied_volatility requires price, S, K, T")

    try:
        price_f = float(price)
        S_f = float(S)
        K_f = float(K)
        T_f = float(T)
        r_f = float(r)
    except Exception as e:
        raise TypeError(f"invalid numeric args: {e}")

    is_call = str(option_type).strip().lower().startswith("c")

    if T_f <= 0 or T_f < 1e-12:
        return 0.0  # 无剩余期限无法反推波动率

    # 内在价值下界：市价不高于此则隐含波动率为 0
    intrinsic = max(S_f - K_f * math.exp(-r_f * T_f), 0.0) if is_call else max(K_f * math.exp(-r_f * T_f) - S_f, 0.0)
    if price_f <= intrinsic + 1e-12:  # 容差避免浮点噪声
        return 0.0
    if price_f <= 0:
        return 0.0

    # 二分区间：1e-6 至 500% 波动率已覆盖绝大多数标的
    low, high = 1e-6, 5.0

    def _price_at(sig):
        return bs_price(S_f, K_f, T_f, r=r_f, sigma=sig, option_type=option_type)

    p_high = _price_at(high)
    # 若上界仍低于市价则封顶返回，避免无限扩张
    if p_high < price_f:
        return float(high)

    for _ in range(100):  # 最多 100 次二分，精度约 1e-6
        mid = 0.5 * (low + high)
        p_mid = _price_at(mid)
        if abs(p_mid - price_f) < 1e-6:  # 价格收敛阈值
            return float(mid)
        if p_mid < price_f:
            low = mid
        else:
            high = mid
        if high - low < 1e-6:  # 波动率收敛阈值
            break
    return float(0.5 * (low + high))


# 别名：兼容不同命名习惯
iv = implied_volatility
implied_vol = implied_volatility
