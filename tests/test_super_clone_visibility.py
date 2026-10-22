"""超级复刻：原商品可见性 / 产品入画前后分段。"""
from backend.services.super_clone_wan3 import (
    STEP1_5_GLOBAL_CONSTRAINT,
    _storyboard_system_prompt,
    first_product_appearance_time,
    group_shots_into_segments,
    invisible_shots_citing_product,
    manifest_for_segment,
    merge_compiled_shot_prompts,
    plan_15s_windows,
    shot_original_product_visible,
    should_split_generation,
    slice_prompt_for_shots,
    split_shots_by_product_appearance,
    wan3_api_duration,
)


def _shot(num, start, end, *, action="无", interact="无", marketing="无", product="无"):
    return {
        "shot": num,
        "时间区间": {"开始时间": start, "结束时间": end},
        "人物与产品": {"人物": "人物", "产品": product},
        "画面动作": {"画面内容": "人物在画面中动作，原商品未出现", "产品动作": action},
        "商品交互": {"交互方式": interact, "营销功能": marketing},
    }


def test_absent_product_shot_is_invisible():
    shot = _shot(1, 0.0, 2.53, product="无")
    assert shot_original_product_visible(shot) is False


def test_named_product_without_action_is_visible():
    shot = _shot(1, 0.0, 2.53, product="目标产品")
    assert shot_original_product_visible(shot) is True


def test_display_shot_is_visible():
    shot = _shot(
        2, 2.53, 5.2,
        action="被折叠持有并展示",
        interact="双手展示",
        marketing="外观展示",
        product="目标产品",
    )
    assert shot_original_product_visible(shot) is True


def test_first_appearance_is_first_visible_shot():
    storyboard = [
        _shot(1, 0.0, 2.53, product="无"),
        _shot(
            2, 2.53, 5.2,
            action="无",
            interact="双手展示",
            marketing="外观展示",
            product="目标产品",
        ),
    ]
    assert first_product_appearance_time(storyboard) == 2.53


def test_model_image_keeps_person_and_product_in_one_generation():
    storyboard = [
        _shot(1, 0.0, 2.53, product="无"),
        _shot(2, 2.53, 8.0, action="展示", interact="手持", product="目标产品"),
    ]
    assert should_split_generation(storyboard, 10.0, model_image_path=None) is True
    assert should_split_generation(storyboard, 10.0, model_image_path="model.jpg") is False


def test_split_assigns_pre_product_shots_to_before_segment():
    storyboard = [
        _shot(1, 0.0, 2.53),
        _shot(
            2, 2.53, 5.2,
            action="被折叠持有并展示",
            interact="双手展示",
            marketing="外观展示",
        ),
        _shot(
            3, 5.2, 8.0,
            action="被使用展示",
            interact="手持使用",
            marketing="使用过程",
        ),
    ]
    before, after = split_shots_by_product_appearance(storyboard, 2.53)
    assert before == {1}
    assert after == {2, 3}


def test_group_shots_keeps_one_shot_per_small_segment():
    storyboard = [
        _shot(1, 0.0, 3.0, product="无"),
        _shot(2, 3.0, 6.5, product="目标产品"),
        _shot(3, 6.5, 11.0, product="目标产品"),
        _shot(4, 11.0, 15.0, product="目标产品"),
    ]
    groups = group_shots_into_segments(storyboard)
    assert [sorted(g["shot_nums"]) for g in groups] == [[1], [2], [3], [4]]
    assert groups[0]["start"] == 0.0
    assert groups[-1]["end"] == 15.0


def test_group_merges_trailing_short_shot():
    storyboard = [
        _shot(1, 0.0, 7.0, product="目标产品"),
        _shot(2, 7.0, 8.2, product="目标产品"),
    ]
    groups = group_shots_into_segments(storyboard)
    assert len(groups) == 1
    assert groups[0]["shot_nums"] == {1, 2}
    assert groups[0]["end"] == 8.2


def test_slice_prompt_rebases_after_segment_times():
    prompt = (
        "Shot 1 [0.00-2.53]: 中景, 人物在画面中动作，原商品未出现;\n"
        "Shot 2 [2.53-5.20]: 中景, 人物展示目标产品 图1;\n"
        f"{STEP1_5_GLOBAL_CONSTRAINT}"
    )
    out = slice_prompt_for_shots(prompt, {2}, time_offset=2.53)
    assert "Shot 1" not in out
    assert "Shot 2 [0.00-2.67]:" in out
    assert out.splitlines()[-1] == STEP1_5_GLOBAL_CONSTRAINT


def test_manifest_before_segment_keeps_model_scene_slots():
    manifest = [
        {"image_id": "图1", "path": "p1.jpg", "label": "产品图 1", "type": "product"},
        {"image_id": "图2", "path": "p2.jpg", "label": "产品图 2", "type": "product"},
        {"image_id": "图3", "path": "model.jpg", "label": "模特图", "type": "model"},
        {"image_id": "图4", "path": "scene.jpg", "label": "场景图", "type": "scene"},
    ]
    before = manifest_for_segment(manifest, include_product=False, product_placeholder_path="still.jpg")
    assert [item["image_id"] for item in before] == ["图1", "图2", "图3", "图4"]
    assert before[0]["path"] == "still.jpg"
    assert before[1]["path"] == "still.jpg"
    assert before[2]["path"] == "model.jpg"
    assert before[3]["path"] == "scene.jpg"


def test_mimo_validate_accepts_marketing_without_observe_type():
    from pipeline.step1_mimo_parse import _validate_result

    shot = {
        "shot": 1,
        "时间区间": {"开始时间": "00:00.00", "结束时间": "00:03.00", "镜头时长": "3.00s"},
        "场景环境": {"场景": "室内", "背景细节": "背景", "光影设定": "自然光"},
        "人物与产品": {"人物": "人物", "产品": "无"},
        "画面动作": {"画面内容": "人物在画面中动作", "人物动作": "动作", "产品动作": "无"},
        "镜头语言": {"景别": "中景", "构图": "居中", "运镜": "固定", "转场衔接": "无"},
        "音效": "无",
        "口播": "无",
        "声音语气": {"语速": "无", "语调": "无", "重音": "无", "停顿": "无", "情绪": "无", "呼吸感": "无"},
        "面部微表情": {"眼神方向": "无", "眉眼变化": "无", "嘴角状态": "无", "表情说明": "无"},
        "身体微动作": {"头部微晃": "无", "重心转移": "无", "手部律动": "无", "肩颈律动": "无"},
        "商品交互": {"交互方式": "无", "交互部位": "无", "营销功能": "无"},
    }
    _validate_result([shot], target_duration=3.0)


def test_scene_swap_decision_truth_table_and_reverse_control():
    """「换背景」这道闸开不开，必须只看语义、不看框不空（2026-10-22 sc_9c246af70384 事故）。"""
    from backend.services.super_clone_wan3 import scene_swap_decision

    assert scene_swap_decision("")[0] is False
    assert scene_swap_decision("   ")[0] is False
    assert scene_swap_decision("白色与米白墙面、浅色原木矮柜、米白布艺沙发")[0] is True

    keep = ("超级复刻这条 14.6 秒竖屏原片：锁死运镜、机位、动作节奏、口播时间与断句，只替换两件事——"
            "① 把原片里的商品换成「植小妞 保湿盈润护理油」；② 把出镜人物换成模特图这张脸。"
            "背景、动作、口播全部保留原片，成片不要字幕花字水印。")
    swap, why = scene_swap_decision(keep)
    assert swap is False and "保留原片" in why          # 原因要说得出是哪句
    # 反向对照：只删掉那句否定，同一份文本必须翻成"要换" ⇒ 红绿由这句话决定，不是由字数决定
    assert scene_swap_decision(keep.replace("背景、动作、口播全部保留原片，", ""))[0] is True


def test_background_predicates_are_not_token_frequency():
    """新判据：新背景看"原话进没进稿"，残留只数 ≥6 字的实元素（旧写法数"前 6 碎词 ≥3 次"，指令型文本必红）。"""
    from backend.services.super_clone_wan3 import background_landed, original_bg_residue

    bg = "白色与米白墙面、浅色原木矮柜、米白布艺沙发、白色亚麻纱帘"
    ok, detail = background_landed(bg, "Shot 1 [00:00.00-00:02.00]: 白色与米白墙面、浅色原木矮柜前，人物展示 图1;")
    assert ok is True
    miss, detail2 = background_landed(bg, "Shot 1 [00:00.00-00:02.00]: 人物展示 图1;")
    assert miss is False and "0" in detail2

    sb = [{"场景环境": {"背景细节": "无其他物体，大面积暖黄褐色墙面，浅色沙发靠背"}}]
    # 空值/否定型（这次误伤的 "无其他物体"）与短片段都不算"原背景元素"
    assert original_bg_residue(bg, sb, "画面为纯白背景，无其他物体，仅有轮廓剪影", []) == []
    # 反向对照：真要换背景时，这两个 ≥6 字的实元素必须被抓出来
    assert original_bg_residue(bg, sb, "大面积暖黄褐色墙面，浅色沙发靠背", []) == [
        "大面积暖黄褐色墙面", "浅色沙发靠背"]


def test_canonical_prompt_tail_pins_the_contract_line_without_deleting_content():
    """合同行由系统钉，不让模型复述 —— 但"钉"只许动那一行。"""
    from backend.services.super_clone_wan3 import canonical_prompt_tail

    same = "Shot 1 [00:00.00-00:02.00]: a;\n" + STEP1_5_GLOBAL_CONSTRAINT
    t, changed, _ = canonical_prompt_tail(same)
    assert changed is False and t == same

    paraph = "Shot 1 [00:00.00-00:02.00]: a;\n" + STEP1_5_GLOBAL_CONSTRAINT[:12] + "后面是模型自己接的话"
    t2, changed2, _ = canonical_prompt_tail(paraph)
    assert changed2 is True
    assert t2.splitlines()[-1] == STEP1_5_GLOBAL_CONSTRAINT      # 尾行钉成精确版
    assert "Shot 1 [00:00.00-00:02.00]: a;" in t2                 # 反向对照：正文一个字不许少

    t3, changed3, _ = canonical_prompt_tail("Shot 1 [00:00.00-00:02.00]: a;")
    assert changed3 is True and t3.splitlines()[-1] == STEP1_5_GLOBAL_CONSTRAINT


def test_bg_red_reissues_only_the_flagged_shots():
    """背景判红后"只重发那几镜"的定位与替换（整段重发在 9 镜时要 173-226s，且输入不变必然再红）。"""
    from backend.services.super_clone_wan3 import replace_shot_line, shot_lines_needing

    p = ("Shot 1 [00:00.00-00:02.00]: 绿色盆栽植物旁;\n"
         "Shot 2 [00:02.00-00:04.00]: 米白墙面;\n"
         "Shot 3 [00:04.00-00:06.00]: 绿色盆栽植物与米白墙面;")
    assert shot_lines_needing(p, ["绿色盆栽植物"]) == [1, 3]
    assert shot_lines_needing(p, ["稿里没有的片段"]) == []        # 空 ⇒ 上层不猜、直接停
    q = replace_shot_line(p, 3, "Shot 3 [00:04.00-00:06.00]: 浅色原木矮柜;")
    assert "浅色原木矮柜" in q and q.count("Shot 1") == 1 and q.count("\n") == 2


def test_storyboard_prompt_is_appended_not_copied_and_pins_duration():
    """分镜腿的补强必须是「受保护原文 + 尾巴」，且时长/数组形状两句必须在尾巴里。

    为什么钉这三件：① 复制一份正文 = 第二个事实源，必然和 step1 漂移；
    ② 传自定义 prompt 会让 run() 走 _is_custom_prompt 分支，user 消息里「目标时长 X 秒」那句消失，
       只剩这里补 ⇒ 丢了它时间轴就覆盖不到 target_duration（Checkpoint A 判红）；
    ③ gen_mode 在 step1 的校验器里是 extra 字段，出现在 Schema 示例里过不了契约。
    """
    from pipeline.step1_mimo_parse import SYSTEM_PROMPT as BASE

    pinned = _storyboard_system_prompt(14.58, "scene_cut")
    assert pinned.startswith(BASE)                    # 派生，不是复制
    tail = pinned[len(BASE):]
    assert "14.58" in tail and "scene_cut" in tail and "gen_mode" in tail

    # 反向对照 1：原文自己不带时长 ⇒ 少了这段补强就真的没人说时长（不是"反正 base 里也有"）
    assert "14.58" not in BASE
    # 反向对照 2：时长来自入参，不是写死的字面量
    other = _storyboard_system_prompt(9.5, "action_grained")
    assert "9.50" in other[len(BASE):] and "14.58" not in other
    assert "action_grained" in other[len(BASE):]


def test_merge_compiled_shot_prompts_keeps_one_constraint():
    merged = merge_compiled_shot_prompts(
        [
            f"Shot 1 [0.00-5.00]: a;\n{STEP1_5_GLOBAL_CONSTRAINT}",
            f"Shot 2 [5.00-15.00]: b;\n{STEP1_5_GLOBAL_CONSTRAINT}",
        ]
    )
    assert merged.splitlines()[0] == "Shot 1 [0.00-5.00]: a;"
    assert merged.splitlines()[1] == "Shot 2 [5.00-15.00]: b;"
    assert merged.splitlines()[-1] == STEP1_5_GLOBAL_CONSTRAINT
    assert merged.count(STEP1_5_GLOBAL_CONSTRAINT) == 1


def test_wan3_api_duration_ceils_fractional_seconds():
    assert wan3_api_duration(2.0) == 2
    assert wan3_api_duration(2.43) == 3
    assert wan3_api_duration(5.0) == 5
    assert wan3_api_duration(1.0) == 2
    assert wan3_api_duration(15.0) == 15


def test_invisible_shot_citing_product_image_is_detected():
    storyboard = [
        _shot(1, 0.0, 2.53, product="无"),
        _shot(2, 2.53, 5.2, action="展示", interact="手持", product="目标产品"),
    ]
    manifest = [
        {"image_id": "图1", "path": "p1.jpg", "label": "产品图 1", "type": "product"},
        {"image_id": "图2", "path": "m.jpg", "label": "模特图", "type": "model"},
    ]
    prompt = (
        "Shot 1 [0.00-2.53]: 中景, 画面中出现目标产品 图1;\n"
        "Shot 2 [2.53-5.20]: 中景, 人物展示目标产品 图1;\n"
        f"{STEP1_5_GLOBAL_CONSTRAINT}"
    )
    assert invisible_shots_citing_product(prompt, storyboard, manifest) == [1]


def _windows_pairs(total):
    return [(w["start"], w["end"]) for w in plan_15s_windows(total)]


def _ref_pairs(total):
    return [(w["ref_start"], w["ref_end"]) for w in plan_15s_windows(total)]


def test_plan_15s_windows_keeps_short_video_as_one_chunk():
    assert _windows_pairs(8.7) == [(0.0, 8.7)]
    assert _windows_pairs(15.0) == [(0.0, 15.0)]
    assert _windows_pairs(15.04) == [(0.0, 15.04)]
    # 单段不制造"第二种读法"：参考窗口就等于本段
    assert _ref_pairs(15.04) == _windows_pairs(15.04)


def test_plan_15s_windows_splits_on_15s_grid():
    assert _windows_pairs(30.0) == [(0.0, 15.0), (15.0, 30.0)]
    assert _windows_pairs(45.0) == [(0.0, 15.0), (15.0, 30.0), (30.0, 45.0)]
    # 2026-10-21 用户口径：40 秒 = 两条满 15 秒(prime) + 一条 10 秒(PIX)，不为了省事出 30/40 秒单段
    assert _windows_pairs(40.0) == [(0.0, 15.0), (15.0, 30.0), (30.0, 40.0)]


def test_plan_15s_windows_borrows_reference_for_short_tail():
    """尾巴短于 PIX 的 5 秒下限时，借的是【参考窗口】不是时间轴。

    旧写法改 start/end（15.13s 被切成 13.13+2.00）⇒ 两段都 <15s ⇒ 双双掉进按秒的 PIX，
    主段本该走满 15 秒的 prime。现在时间轴照旧连续，只有喂模型的那一份往前借满 5s。
    """
    assert _windows_pairs(15.13) == [(0.0, 15.0), (15.0, 15.13)]
    assert _ref_pairs(15.13) == [(0.0, 15.0), (10.13, 15.13)]
    assert _windows_pairs(16.0) == [(0.0, 15.0), (15.0, 16.0)]
    assert _ref_pairs(16.0) == [(0.0, 15.0), (11.0, 16.0)]
    assert _ref_pairs(31.0) == [(0.0, 15.0), (15.0, 30.0), (26.0, 31.0)]
    # 尾巴本来就 ≥5s ⇒ 不借、不重叠（22.5s 的尾段是 7.5s）
    assert _ref_pairs(22.5) == _windows_pairs(22.5)


def test_plan_15s_windows_covers_full_duration_without_gaps():
    for total in (1.0, 15.0, 15.13, 16.0, 22.5, 31.0, 45.2, 60.0):
        wins = plan_15s_windows(total)
        assert wins[0]["start"] == 0.0
        assert abs(wins[-1]["end"] - total) < 1e-6
        for i in range(1, len(wins)):
            # 时间轴连续、不重叠、不丢帧 —— 借的只是参考窗口
            assert abs(wins[i]["start"] - wins[i - 1]["end"]) < 1e-6
        for w in wins:
            assert (w["end"] - w["start"]) <= 15.05
            assert (w["end"] - w["start"]) <= (w["ref_end"] - w["ref_start"]) + 1e-6
            assert 0.0 <= w["ref_start"] < w["ref_end"] <= total + 1e-6
            # 喂模型的参考窗口：要么就是本段，要么 ≥5s（PIX 下限），且同样不许超 15s
            ref_dur = w["ref_end"] - w["ref_start"]
            assert ref_dur <= 15.05
            assert abs(ref_dur - (w["end"] - w["start"])) < 1e-6 or ref_dur >= 5.0 - 0.05


def test_delivery_problems_requires_a_file_on_disk(tmp_path):
    from backend.services.super_clone_wan3 import delivery_problems
    real = tmp_path / "seg01-prime.mp4"
    real.write_bytes(b"abc")
    assert delivery_problems({"duration_status": "pass", "final_path": str(real)}) == []
    assert delivery_problems({"duration_status": "trimmed", "final_path": str(real)}) == []
    # 现网 sc_9c246af70384 的形状：提交被拒 ⇒ 没成片，原来照样打「工作流完成 → None」+ SUCCESS
    bad = delivery_problems({"duration_status": "generation_failed", "final_path": None})
    assert len(bad) == 2
    assert len(delivery_problems({})) == 2
    assert delivery_problems(None) == ["执行器没返回信封，无从判定交付"]
    # 反向对照：状态自称 pass 但文件不在盘上 ⇒ 一样不许过（只看终态会靠这句假绿）
    gone = delivery_problems({"duration_status": "pass", "final_path": str(tmp_path / "nope.mp4")})
    assert len(gone) == 1 and "不在盘上" in gone[0]


def test_prime_env_rejection_reason_is_not_called_param_error():
    from backend.services.wan3_client import (WanApprovalRequired, WanParamError, WanTimeoutError,
                                              WanUpstreamUnavailable, _raise_prime_env_failure)
    try:
        _raise_prime_env_failure({"ok": False, "error": "approval_required：默认要人批准",
                                  "approval": {"reason": "approval_required"}}, "")
    except WanApprovalRequired as e:
        assert "0 分未扣" in str(e)
    else:
        raise AssertionError("批准闸的 0 分拒绝没抛 WanApprovalRequired")
    # 现网 23:51 的真信封（1080P 那条线渠道池空）：上游没渠道 ≠ 我方参数写错
    real_503 = ("wan3-prime：提交被拒 HTTP 500：fail_to_fetch_task：{\"object\":\"video\","
                "\"status\":\"failed\",\"error\":{\"message\":\"上游创建任务失败，状态码:503\","
                "\"code\":\"task_failed\"},\"detail\":\"{\\\"error\\\":{\\\"code\\\":"
                "\\\"model_not_found\\\",\\\"message\\\":\\\"No available channel for model "
                "w3-p-c1 under group default (distributor)\\\"}}\"}")
    try:
        _raise_prime_env_failure({"ok": False, "error": real_503}, "")
    except WanUpstreamUnavailable as e:
        assert "上游没有可用渠道" in str(e)
    else:
        raise AssertionError("上游 503 没渠道被归成了别的（原来谎报成『API 参数非法（400）』）")
    # 反向对照：老的几种分类不能被新分支吞掉
    for env, jid, want in (
        ({"ok": False, "error": "参数非法 HTTP 400: bad ratio"}, "", WanParamError),
        ({"ok": False, "error": "wan3-prime ratio 只认 adaptive/9:16（收到 '4:5'）"}, "", WanParamError),
        ({"ok": False, "error": "上游超时，结果未知"}, "job-1", WanTimeoutError),
    ):
        try:
            _raise_prime_env_failure(env, jid)
        except want:
            continue
        raise AssertionError("这条没归类成 %s" % want.__name__)


def test_approval_rejection_does_not_burn_a_generation_attempt():
    # 重试环要花钱才能跑 ⇒ 用 AST 钉住【不重试白名单】这个形状，不钉字面排版、也不钉函数名
    # （钉排版会在换行时假红；钉函数名会跟着一次改名假红——本仓 step2_wan3_execute 就是这种形状）。
    import ast
    from pathlib import Path

    tree = ast.parse(Path("backend/services/super_clone_wan3.py").read_text(encoding="utf-8"))
    permanent = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.In)
                and isinstance(node.comparators[0], ast.Tuple)):
            vals = {e.value for e in node.comparators[0].elts if isinstance(e, ast.Constant)}
            if "WanAuthError" in vals:
                permanent = vals
                break
    assert permanent, "找不到【不重试】的 Wan 错误名单（那条 if 被改坏了？）"
    for want in ("WanApprovalRequired", "WanUpstreamUnavailable"):
        assert want in permanent, "%s 不在【不重试】名单里 ⇒ 会白烧 attempt" % want
    # 名单里每一项都得有一句人话：漏一项 = except 里 KeyError，把"为什么没出片"炸成异常
    reason_keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            reason_keys |= {k.value for k in node.keys if isinstance(k, ast.Constant)
                            and str(k.value).startswith("Wan")}
    assert not (permanent - reason_keys), "这些 Wan 错误类没有对应拒因文案：%s" % sorted(permanent - reason_keys)
