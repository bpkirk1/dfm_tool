from app.engine.operators import apply_operator


def test_lte_pass_and_fail():
    assert apply_operator("lte", 0.03, 0.04).satisfied is True
    assert apply_operator("lte", 0.05, 0.04).satisfied is False


def test_gte_pass_and_fail():
    assert apply_operator("gte", 0.20, 0.15).satisfied is True
    assert apply_operator("gte", 0.10, 0.15).satisfied is False


def test_lt_gt_strict():
    assert apply_operator("lt", 0.04, 0.04).satisfied is False
    assert apply_operator("gt", 0.04, 0.04).satisfied is False


def test_eq_numeric_and_text():
    assert apply_operator("eq", 1.0, 1.0).satisfied is True
    # free-text callout cannot be auto-evaluated -> manual
    assert apply_operator("eq", 1.0, "0.036 x 45deg, farside").satisfied is None


def test_between():
    assert apply_operator("between", 5, [1, 10]).satisfied is True
    assert apply_operator("between", 11, [1, 10]).satisfied is False


def test_angle_tol_asymmetric():
    # 135 +8/-1 -> band [134, 143]
    limit = {"target": 135, "plus": 8, "minus": 1}
    assert apply_operator("angle_tol", 140, limit).satisfied is True
    assert apply_operator("angle_tol", 133, limit).satisfied is False
    assert apply_operator("angle_tol", 144, limit).satisfied is False


def test_unmeasurable_value_is_none():
    assert apply_operator("lte", None, 0.04).satisfied is None


def test_margin_sign():
    out = apply_operator("lte", 0.03, 0.04)
    assert out.margin > 0  # comfortably inside
    out2 = apply_operator("lte", 0.05, 0.04)
    assert out2.margin < 0  # violation
