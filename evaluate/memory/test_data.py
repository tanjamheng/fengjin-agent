"""记忆系统评估测试数据集

包含所有评估用例，分为写入阶段和检索阶段。
"""

# ============================================================
# 写入阶段：提取测试数据
# ============================================================

EXTRACTION_TEST = [
    {
        "id": "ext-01",
        "user": "我最近换工作了，现在在 Google 做 SRE",
        "assistant": "恭喜你！在 Google 做 SRE 听起来很棒呀",
        "ground_truths": ["灰宝在Google做SRE", "灰宝换了工作"],
        "category": "职业信息",
    },
    {
        "id": "ext-02",
        "user": "我对花生过敏",
        "assistant": "记住啦，你对花生过敏，我会注意的",
        "ground_truths": ["灰宝对花生过敏"],
        "expected_importance": "high",
        "category": "健康禁忌",
    },
    {
        "id": "ext-03",
        "user": "早上好",
        "assistant": "早上好呀灰宝！今天天气不错呢",
        "ground_truths": [],
        "category": "寒暄",
    },
    {
        "id": "ext-04",
        "user": "请记住我的生日是5月20日",
        "assistant": "好的，记住啦，你的生日是5月20日",
        "ground_truths": ["灰宝的生日是5月20日"],
        "expected_importance": "high",
        "category": "显式记忆请求",
    },
    {
        "id": "ext-05",
        "user": "我今天去爬山了，风景很美",
        "assistant": "哇，爬山好棒呀！看到什么风景了？",
        "ground_truths": ["灰宝去爬山了"],
        "expected_importance": "low",
        "category": "日常活动",
    },
    {
        "id": "ext-06",
        "user": "我养了一只橘猫叫小橘",
        "assistant": "小橘好可爱！橘猫都特别亲人呢",
        "ground_truths": ["灰宝养了一只橘猫叫小橘"],
        "category": "宠物信息",
    },
    {
        "id": "ext-07",
        "user": "我不喜欢下雨天，心情会变差",
        "assistant": "理解你，下雨天确实容易让人低落",
        "ground_truths": ["灰宝不喜欢下雨天"],
        "category": "情绪偏好",
    },
    {
        "id": "ext-08",
        "user": "我在学水彩画，最近在画风景",
        "assistant": "学水彩画好有品味！画风景一定很美",
        "ground_truths": ["灰宝在学水彩画", "灰宝最近在画风景"],
        "category": "兴趣爱好",
    },
    {
        "id": "ext-09",
        "user": "我对海鲜也过敏，虾和蟹都不能吃",
        "assistant": "记住啦，海鲜过敏，虾和蟹都不能吃",
        "ground_truths": ["灰宝对海鲜过敏", "灰宝不能吃虾和蟹"],
        "expected_importance": "high",
        "category": "健康禁忌",
    },
    {
        "id": "ext-10",
        "user": "我最喜欢的颜色是蓝色",
        "assistant": "蓝色很好看，像天空和大海的颜色",
        "ground_truths": ["灰宝最喜欢的颜色是蓝色"],
        "category": "偏好信息",
    },
]

# ============================================================
# 写入阶段：重要性判断测试数据
# ============================================================

IMPORTANCE_TEST = [
    {"id": "imp-01", "input": "我对花生过敏", "expected": "high", "reason": "过敏信息"},
    {"id": "imp-02", "input": "请记住我的生日是5月20日", "expected": "high", "reason": "显式要求记住"},
    {"id": "imp-03", "input": "我今天吃了火锅", "expected": "low", "reason": "日常活动"},
    {"id": "imp-04", "input": "我喜欢猫", "expected": "low", "reason": "普通偏好"},
    {"id": "imp-05", "input": "我有糖尿病，不能吃太多糖", "expected": "high", "reason": "疾病饮食限制"},
    {"id": "imp-06", "input": "我最近在学画画", "expected": "low", "reason": "兴趣爱好"},
    {"id": "imp-07", "input": "记住我是素食主义者", "expected": "high", "reason": "显式要求+饮食限制"},
    {"id": "imp-08", "input": "我今天心情不太好", "expected": "low", "reason": "临时情绪"},
]

# ============================================================
# 写入阶段：去重测试数据
# ============================================================

DEDUP_TEST = {
    "initial": [
        {"content": "灰宝喜欢猫", "type": "semantic", "importance": "low"},
        {"content": "灰宝对花生过敏", "type": "semantic", "importance": "high"},
    ],
    "duplicates": [
        {"content": "灰宝喜欢猫咪", "type": "semantic", "importance": "low"},
        {"content": "灰宝花生过敏", "type": "semantic", "importance": "high"},
    ],
    "new_facts": [
        {"content": "灰宝最近在学画画", "type": "semantic", "importance": "low"},
        {"content": "灰宝养了一只狗", "type": "semantic", "importance": "low"},
    ],
}

# ============================================================
# 写入阶段：冲突测试数据
# ============================================================

CONFLICT_TEST = [
    {
        "id": "conflict-01",
        "initial": {"content": "灰宝喜欢猫", "type": "semantic", "importance": "low"},
        "conflict": {"content": "灰宝不再喜欢猫了，现在喜欢狗", "type": "semantic", "importance": "low"},
        "expected_behavior": "merge_or_coexist",
        "category": "偏好变化",
    },
    {
        "id": "conflict-02",
        "initial": {"content": "灰宝在Google做SRE", "type": "semantic", "importance": "low"},
        "conflict": {"content": "灰宝跳槽到字节跳动了，做后端开发", "type": "semantic", "importance": "low"},
        "expected_behavior": "merge_or_coexist",
        "category": "职业变化",
    },
]

# ============================================================
# 写入阶段：PII 过滤测试数据
# ============================================================

PII_TEST = [
    {"id": "pii-01", "user": "我的手机号是13812345678", "assistant": "好的，我记一下", "contains_pii": True},
    {"id": "pii-02", "user": "我的密码是abc123", "assistant": "好的", "contains_pii": True},
    {"id": "pii-03", "user": "我的身份证号是110101199001011234", "assistant": "记住啦", "contains_pii": True},
    {"id": "pii-04", "user": "我喜欢蓝色", "assistant": "蓝色很好看", "contains_pii": False},
]

# ============================================================
# 检索阶段：检索测试数据（需要先写入初始记忆）
# ============================================================

RETRIEVAL_INITIAL_MEMORIES = [
    {"id": "ret-m-1", "content": "灰宝对花生过敏", "is_core": True, "memory_type": "semantic"},
    {"id": "ret-m-2", "content": "灰宝最近在学水彩画", "is_core": False, "memory_type": "episodic"},
    {"id": "ret-m-3", "content": "灰宝喜欢猫", "is_core": False, "memory_type": "semantic"},
    {"id": "ret-m-4", "content": "灰宝在Google做SRE", "is_core": False, "memory_type": "semantic"},
    {"id": "ret-m-5", "content": "灰宝最喜欢的颜色是蓝色", "is_core": False, "memory_type": "semantic"},
    {"id": "ret-m-6", "content": "灰宝养了一只橘猫叫小橘", "is_core": False, "memory_type": "semantic"},
    {"id": "ret-m-7", "content": "灰宝不喜欢下雨天", "is_core": False, "memory_type": "semantic"},
    {"id": "ret-m-8", "content": "灰宝对海鲜过敏，不能吃虾和蟹", "is_core": True, "memory_type": "semantic"},
]

RETRIEVAL_TEST = [
    {
        "id": "ret-01",
        "query": "灰宝过敏什么",
        "relevant_contents": ["花生过敏", "海鲜过敏"],
        "category": "健康信息",
    },
    {
        "id": "ret-02",
        "query": "灰宝在学什么",
        "relevant_contents": ["学水彩画"],
        "category": "兴趣爱好",
    },
    {
        "id": "ret-03",
        "query": "灰宝喜欢什么动物",
        "relevant_contents": ["喜欢猫", "橘猫叫小橘"],
        "category": "偏好信息",
    },
    {
        "id": "ret-04",
        "query": "灰宝在哪里工作",
        "relevant_contents": ["Google做SRE"],
        "category": "职业信息",
    },
    {
        "id": "ret-05",
        "query": "灰宝喜欢什么颜色",
        "relevant_contents": ["蓝色"],
        "category": "偏好信息",
    },
    {
        "id": "ret-06",
        "query": "灰宝能吃虾吗",
        "relevant_contents": ["海鲜过敏", "不能吃虾"],
        "category": "健康信息",
    },
    {
        "id": "ret-07",
        "query": "灰宝的猫叫什么",
        "relevant_contents": ["橘猫叫小橘"],
        "category": "宠物信息",
    },
    {
        "id": "ret-08",
        "query": "灰宝讨厌什么天气",
        "relevant_contents": ["不喜欢下雨天"],
        "category": "偏好信息",
    },
    {
        "id": "ret-09",
        "query": "今天天气怎么样",
        "relevant_contents": [],
        "category": "无关查询",
    },
    {
        "id": "ret-10",
        "query": "你还记得关于灰宝的一切吗",
        "relevant_contents": ["花生过敏", "水彩画", "猫", "蓝色", "Google"],
        "category": "综合查询",
    },
]
