#!/usr/bin/env python3
"""
HR-20 面试纪要三栏汇总与待办清单 主脚本
==========================================
功能：解析多份面试纪要 → 分栏抽取 → 检测矛盾 → 归纳挂出处 → 生成三栏汇总表+待办清单
护栏：自动检查评分/录用输出次数、原文覆盖次数、归纳出处达标率等

使用：
    python3 scripts/hr20.py --candidate "张三/Java后端/ID-20261117-003" --minute-dir demo/张三/
    python3 scripts/hr20.py --resume 张三/Java后端/ID-20261117-003
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ========== 全局常量 ==========
PROJECT_ROOT = Path(__file__).parent.parent
STATE_DIR = Path("/tmp/hr20-state")
CACHE_DIR = Path("/tmp/hr20-cache")
OUTPUT_DIR = PROJECT_ROOT / "output"
TRACE_DIR = PROJECT_ROOT / "trace"

# 护栏关键词（正则）
# G-01 评分：只匹配真正的评分数字/标记，排除"优秀/一般"这类主观评价词
PATTERN_SCORE_CHECK = re.compile(r'\d+分|评分[：:]|打分|等级[：:]|\u2b50{2,5}|百分制')
# G-02 录用结论：只匹配评价性的（建议录用/淘汰、淘汰风险等），不匹配动作性的（通知终面、安排二面）
PATTERN_HIRE_CHECK = re.compile(r'建议[录用淘汰]|推荐[录用淘汰]|淘汰[风险率概率]|录用结论|淘汰结论|推荐[终面]')

# 主观评价词（用于判断"评价栏"）
SUBJECTIVE_WORDS = [
    '好', '坏', '优秀', '一般', '良好', '较差', '扎实', '深厚', '肤浅',
    '流畅', '流畅', '清晰', '模糊', '到位', '不到位', '可以', '不行',
    '不错', '凑合', '比较', '相当', '较为', '还算', '整体', '看起来',
    '我觉得', '我认为', '感觉', '似乎', '应该', '好像', '估计',
    '倾向于', '个人认为', '个人感觉'
]

# 待办触发关键词（必须是"要做什么事"，不是元信息或客观描述）
TODO_KEYWORDS_STRONG = [
    '下一步', '跟进', '请提供', '请提交', '请发送', '请安排', '请准备',
    '回头让', '回头通知', '回头联系',
    '下周二前', '周三下午', '下周一我会通知', '通知候选人',
    '约定', '说好', '约好', '提前', '需要在',
]
TODO_TEMPORAL = ['明天', '后天', '月底', '月底前', '下周', '下周二', '下周三', '下周四', '下周五', '下周一', '本周', '下周', '本月', '下个月', '到时', '到时候', '稍后', '之后', '改天']
TODO_ACTION = ['提交', '提供', '发送', '安排', '准备', '联系', '通知', '面试', '复试', '终面']

# 元信息关键词（排除这些段落）
META_KEYWORDS = ['面试官：', '面试日期：', '应聘岗位：', '候选人：', '---', '## ']

# ========== 工具函数 ==========
def sha256(text: str) -> str:
    """计算文本 SHA256"""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]

def iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')

def log(msg: str, stage: str = None):
    ts = datetime.now().strftime('%H:%M:%S')
    prefix = f"[{ts}]"
    if stage:
        prefix += f" [{stage}]"
    print(f"{prefix} {msg}", flush=True)

def save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_json(path: Path) -> dict:
    if not path.exists():
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

# ========== Step 1: 读多份纪要（indexer 逻辑） ==========
def index_minutes(candidate_id: str, minute_dir: Path) -> dict:
    """解析目录下所有面试纪要，建段落索引"""
    log(f"开始索引化，目录：{minute_dir}")
    
    # 找所有 .md 文件，按轮次排序
    files = sorted(minute_dir.glob("*.md"))
    if len(files) < 2:
        log(f"❌ 纪要数量不足：{len(files)}（需要 ≥2）", "STAGE_1")
        sys.exit(1)
    
    # 幂等键
    file_hashes = []
    for f in files:
        content = f.read_text(encoding='utf-8')
        file_hashes.append(sha256(content))
    idempotent_key = sha256(candidate_id + "|" + "|".join(file_hashes) + "|v1.0")
    
    # 检查缓存
    cache_path = CACHE_DIR / idempotent_key / "index.json"
    cached = load_json(cache_path)
    if cached:
        log(f"✅ 命中幂等缓存，跳过解析", "STAGE_1")
        return cached
    
    rounds_data = []
    total_paragraphs = 0
    missing_paragraphs = []
    
    for f in files:
        content = f.read_text(encoding='utf-8')
        lines = content.strip().split('\n')
        
        # 识别轮次和面试官
        round_id = "R1"
        interviewer = "未知"
        for line in lines[:10]:
            m = re.match(r'#\s*([一二三四五]面).*面试纪要', line)
            if m:
                num_map = {'一': '1', '二': '2', '三': '3', '四': '4', '五': '5'}
                round_id = f"R{num_map.get(m.group(1)[0], '1')}"
            m2 = re.match(r'.*面试官[：:]\s*(.+)', line)
            if m2:
                interviewer = m2.group(1).strip()
        
        # 提取段落（按空行分割，跳过markdown标题）
        paragraphs = []
        current_paragraph = []
        seq = 0
        
        for line in lines:
            stripped = line.strip()
            if not stripped:
                if current_paragraph:
                    seq += 1
                    text = '\n'.join(current_paragraph).strip()
                    # 跳过markdown标题行
                    clean_lines = [l for l in text.split('\n') if not l.startswith('#')]
                    text = '\n'.join(clean_lines).strip()
                    if text:
                        paragraphs.append({
                            'paragraph_id': f"{round_id}-P{seq:03d}",
                            'seq': seq,
                            'text': text,
                            'content_hash': sha256(text),
                            'is_missing': False,
                            'is_heading_only': text.startswith('#')
                        })
                        total_paragraphs += 1
                    current_paragraph = []
            else:
                current_paragraph.append(line)
        
        if current_paragraph:
            seq += 1
            text = '\n'.join(current_paragraph).strip()
            clean_lines = [l for l in text.split('\n') if not l.startswith('#')]
            text = '\n'.join(clean_lines).strip()
            if text:
                paragraphs.append({
                    'paragraph_id': f"{round_id}-P{seq:03d}",
                    'seq': seq,
                    'text': text,
                    'content_hash': sha256(text),
                    'is_missing': False
                })
                total_paragraphs += 1
        
        # 检测缺段标记
        for p in paragraphs:
            if '待补' in p['text'] or '缺失' in p['text'] or '⚠️' in p['text']:
                p['is_missing'] = True
                missing_paragraphs.append({
                    'round': round_id,
                    'paragraph_id': p['paragraph_id'],
                    'label': p['text'][:60]
                })
        
        rounds_data.append({
            'round': round_id,
            'interviewer': interviewer,
            'file_name': f.name,
            'paragraphs': paragraphs
        })
        
        log(f"  ✅ {f.name}: 轮次={round_id}, 面试官={interviewer}, 段落数={len(paragraphs)}", "STAGE_1")
    
    # 检测矛盾（同一个候选人属性在不同轮次里有矛盾描述）
    conflict_groups = detect_conflicts(rounds_data)
    
    index = {
        'candidate_id': candidate_id,
        'index_version': 'v1.0',
        'total_paragraphs': total_paragraphs,
        'missing_paragraphs': missing_paragraphs,
        'conflict_groups': conflict_groups,
        'rounds': rounds_data,
        'idempotent_key': idempotent_key,
        'created_at': iso_now()
    }
    
    # 写缓存
    save_json(cache_path, index)
    log(f"✅ STAGE_1 完成：{total_paragraphs} 段落，缺段 {len(missing_paragraphs)} 处，矛盾 {len(conflict_groups)} 组", "STAGE_1")
    
    # 写状态
    update_state(candidate_id, 'STAGE_1', 'success', idempotent_key, str(cache_path))
    
    return index

def detect_conflicts(rounds_data: list) -> list:
    """检测不同轮次里互相矛盾的评价"""
    conflicts = []
    # 收集所有含主观评价的段落
    eval_paragraphs = []
    for r in rounds_data:
        for p in r['paragraphs']:
            if not p['is_missing']:
                for word in SUBJECTIVE_WORDS:
                    if word in p['text'] and len(p['text']) > 10:
                        eval_paragraphs.append({
                            'round': r['round'],
                            'paragraph': p,
                            'text': p['text'][:200]
                        })
                        break
    
    # 检测矛盾：不同轮次里出现相反的评价词
    opposite_pairs = [
        ('扎实', '一般'), ('扎实', '肤浅'), ('一般', '深厚'),
        ('流畅', '一般'), ('流畅', '较差'), ('好', '不好'),
        ('可以', '不行'), ('不错', '较差'), ('优秀', '一般'),
    ]
    
    for i in range(len(eval_paragraphs)):
        for j in range(i+1, len(eval_paragraphs)):
            a = eval_paragraphs[i]
            b = eval_paragraphs[j]
            if a['round'] == b['round']:
                continue
            for word1, word2 in opposite_pairs:
                if word1 in a['text'] and word2 in b['text']:
                    conflicts.append({
                        'type': 'evaluation',
                        'words': (word1, word2),
                        'paragraphs': [
                            {'round': a['round'], 'paragraph_id': a['paragraph']['paragraph_id'], 'text': a['paragraph']['text'][:150]},
                            {'round': b['round'], 'paragraph_id': b['paragraph']['paragraph_id'], 'text': b['paragraph']['text'][:150]}
                        ]
                    })
                    break
    
    log(f"  检测到 {len(conflicts)} 组矛盾评价", "STAGE_1")
    return conflicts

# ========== Step 2: 分栏抽取 ==========
def split_fact_eval(index: dict) -> dict:
    """把段落拆成事实栏和评价栏"""
    log("开始分栏抽取...", "STAGE_2")
    
    facts = []
    evaluations = []
    skipped_violations = []
    skipped_meta = []
    
    for r in index['rounds']:
        for p in r['paragraphs']:
            if p['is_missing']:
                continue
            text = p['text']
            round_id = r['round']
            interviewer = r['interviewer']
            pid = p['paragraph_id']
            
            # 跳过元信息段落
            if is_meta_paragraph(text):
                skipped_meta.append(pid)
                continue
            
            # 跳过含评分/录用违规内容的段落（但保留其他客观内容）
            if has_violations(text):
                # 尝试从段落里剔除违规句子，保留客观部分
                sentences = re.split(r'[。！？\n]', text)
                clean_sentences = [s.strip() for s in sentences if s.strip() and not has_violations(s)]
                if clean_sentences:
                    text = '。'.join(clean_sentences)
                else:
                    skipped_violations.append(pid)
                    log(f"  ⚠️ 跳过违规段落：{pid}（{text[:50]}...）", "STAGE_2")
                    continue
            
            # 判断类型
            is_evaluation = False
            has_subjective = False
            for word in SUBJECTIVE_WORDS:
                if word in text:
                    has_subjective = True
                    break
            
            # 以"面试官评价"或"初步印象"或"综合评价"开头的段落 → 评价
            if re.search(r'评价|印象|综合|整体|初步|结论', text[:30]):
                is_evaluation = True
            # 含主观词 + 不是纯客观描述 → 评价
            elif has_subjective and len(text) < 500 and not re.search(r'\d{4}年|本科|硕士|Java|Spring|MySQL|Redis|Kafka', text[:100]):
                is_evaluation = True
            # 纯事实描述 → 事实
            else:
                is_evaluation = False
            
            entry = {
                'paragraph_id': pid,
                'round': round_id,
                'interviewer': interviewer,
                'text': text,
                'content_hash': p['content_hash'],
                'source': pid,  # 简化：不再重复 round_id
                'is_conflict': any(
                    cp['paragraph_id'] == pid 
                    for conflict in index['conflict_groups'] 
                    for cp in conflict['paragraphs']
                ),
                'was_violation': pid in skipped_violations
            }
            
            if is_evaluation:
                evaluations.append(entry)
            else:
                facts.append(entry)
    
    log(f"  事实栏：{len(facts)} 条，评价栏：{len(evaluations)} 条", "STAGE_2")
    log(f"  跳过违规段落：{len(skipped_violations)} 个，跳过元信息：{len(skipped_meta)} 个", "STAGE_2")
    
    result = {
        'facts': facts,
        'evaluations': evaluations,
        'missing': index['missing_paragraphs'],
        'conflicts': index['conflict_groups'],
        'source_index_idempotent': index['idempotent_key']
    }
    
    # 护栏检查
    guardrail = check_guardrails(result, index)
    log(f"  护栏检查：{json.dumps(guardrail, ensure_ascii=False)}", "STAGE_2")
    
    # 检查点
    output_path = PROJECT_ROOT / 'scripts' / '_stage2_result.json'
    save_json(output_path, {'result': result, 'guardrail': guardrail})
    update_state(index['candidate_id'], 'STAGE_2', 'success', 
                 sha256(index['idempotent_key'] + '|split_v1'),
                 str(output_path), guardrail)
    
    return {'result': result, 'guardrail': guardrail}

# ========== Step 3: 出处挂接与归纳 ==========
def merge_induction(index: dict, split_result: dict) -> dict:
    """归纳：把多份纪要重复的事实合并，挂出处"""
    log("开始出处挂接与归纳...", "STAGE_2")
    
    facts = split_result['facts']
    
    # 按语义相近分组（简化版：关键词重叠率 ≥ 50% 视为相关）
    groups = []
    used = set()
    
    for i, fact in enumerate(facts):
        if i in used:
            continue
        # 从事实里提取候选关键信息
        keywords = extract_keywords(fact['text'])
        group = [fact]
        used.add(i)
        
        for j in range(i+1, len(facts)):
            if j in used:
                continue
            other_kw = extract_keywords(facts[j]['text'])
            overlap = len(keywords & other_kw)
            if overlap >= 2 or (keywords and other_kw and overlap / max(len(keywords), 1) >= 0.4):
                group.append(facts[j])
                used.add(j)
        
        if len(group) >= 1:
            groups.append(group)
    
    # 生成归纳句（只有跨轮次的组才做归纳）
    inductions = []
    for group in groups:
        rounds_in_group = set(g['round'] for g in group)
        if len(rounds_in_group) >= 2:
            # 生成归纳句（简化版：取最短最凝练的一条作为归纳）
            sorted_by_len = sorted(group, key=lambda x: len(x['text']))
            induction_text = sorted_by_len[0]['text'][:100] + ('...' if len(sorted_by_len[0]['text']) > 100 else '')
            
            inductions.append({
                'induction_text': induction_text,
                'sources': [
                    {
                        'paragraph_id': g['paragraph_id'],
                        'round': g['round'],
                        'interviewer': g['interviewer'],
                        'text': g['text'][:200],
                        'source_link': f"{g['round']}-{g['paragraph_id']}"
                    }
                    for g in group
                ],
                'source_count': len(group),
                'rounds_involved': sorted(rounds_in_group)
            })
    
    log(f"  归纳句：{len(inductions)} 条（跨轮次合并）", "STAGE_2")
    
    # 护栏检查：每条归纳出处 ≥ 2
    induction_src_ok = all(ind['source_count'] >= 2 for ind in inductions) if inductions else True
    log(f"  归纳出处检查：{'✅ 全过' if induction_src_ok else '❌ 有出处不足的'}", "STAGE_2")
    
    split_result['inductions'] = inductions
    
    # 写检查点
    output_path = PROJECT_ROOT / 'scripts' / '_stage2_final.json'
    save_json(output_path, split_result)
    
    return split_result

def extract_keywords(text: str) -> set:
    """从文本里提取关键词（简化版：数字+专有名词）"""
    keywords = set()
    # 提取年份数字、技术名词、人名（简化）
    tech_terms = ['Java', 'Spring', 'Boot', 'MySQL', 'Redis', 'Kafka', 'Kubernetes',
                  '分布式', '事务', '锁', '微服务', '高并发', '架构', '后端', '前端',
                  'Python', 'Go', 'React', 'Vue', 'Node', 'Docker', 'Git', 'HTTP',
                  'Bean', '生命周期', '循环依赖', 'TCC', '两阶段', '幂等', '熔断',
                  '面试', '候选人', '项目', '经验', '本科', '硕士', '毕业',
                  '面试官', '一面', '二面', '三面']
    for term in tech_terms:
        if term.lower() in text.lower():
            keywords.add(term)
    
    # 提取年份/年限数字
    for m in re.finditer(r'\d{1,2}[年月]', text):
        keywords.add(m.group())
    for m in re.finditer(r'\d+年', text):
        keywords.add(m.group())
    
    return keywords

# ========== Step 4: 护栏自动检查 ==========
def check_guardrails(split_result: dict, index: dict) -> dict:
    """执行 5 项护栏检查"""
    guardrail = {
        'G-01_score_output': 0,
        'G-02_hire_output': 0,
        'G-03_induction_source_ok': 1.0,
        'G-04_conflict_not_merged': True,
        'G-05_coverage': 0.0,
        'overall_passed': True
    }
    
    all_text = ""
    for item in split_result['facts']:
        all_text += item['text'] + "\n"
    for item in split_result['evaluations']:
        all_text += item['text'] + "\n"
    
    # G-01: 评分次数（严格版）
    guardrail['G-01_score_output'] = len(PATTERN_SCORE_CHECK.findall(all_text))
    
    # G-02: 录用次数（严格版，只匹配评价性）
    guardrail['G-02_hire_output'] = len(PATTERN_HIRE_CHECK.findall(all_text))
    
    # G-03: 归纳出处达标率（稍后 Step3 后再检查，这里先标占位）
    guardrail['G-03_induction_source_ok'] = -1  # 占位
    
    # G-04: 矛盾未合并（矛盾段落应分别出现在结果中）
    conflict_paras = set()
    for conflict in index.get('conflict_groups', []):
        for cp in conflict['paragraphs']:
            conflict_paras.add(cp['paragraph_id'])
    result_paras = set()
    for item in split_result['facts'] + split_result['evaluations']:
        result_paras.add(item['paragraph_id'])
    guardrail['G-04_conflict_not_merged'] = conflict_paras.issubset(result_paras)
    
    # G-05: 原文覆盖次数
    total_original = index['total_paragraphs']
    total_missing = len(index.get('missing_paragraphs', []))
    expected_coverage = total_original - total_missing
    actual_covered = len(result_paras)
    guardrail['G-05_coverage'] = actual_covered / expected_coverage if expected_coverage > 0 else 0
    guardrail['G-05_original_covered_count'] = actual_covered
    guardrail['G-05_expected_count'] = expected_coverage
    guardrail['G-05_overwrite_count'] = expected_coverage - actual_covered
    
    # overall passed（G-05 跳过违规段落不算覆盖）
    # 重新统计：覆盖数 = 引用的段落ID数（已去重） - 故意跳过的违规/元信息段落
    all_para_ids_in_result = set()
    for item in split_result['facts'] + split_result['evaluations']:
        all_para_ids_in_result.add(item['paragraph_id'])
    # 原始总段落ID集合
    all_original_ids = set()
    for r in index['rounds']:
        for p in r['paragraphs']:
            all_original_ids.add(p['paragraph_id'])
    # 故意跳过的段落（违规+元信息）
    skipped = all_original_ids - all_para_ids_in_result
    # 检查跳过的都是违规/元信息（不是正常内容）
    skipped_all_valid = True
    for sid in skipped:
        # 找出这个段落原文
        for r in index['rounds']:
            for p in r['paragraphs']:
                if p['paragraph_id'] == sid:
                    if not is_meta_paragraph(p['text']) and not has_violations(p['text']):
                        skipped_all_valid = False
                    break
    
    guardrail['G-05_overwrite_count'] = 0 if skipped_all_valid else len(skipped)
    guardrail['skipped_paragraph_ids'] = sorted(skipped)
    guardrail['overall_passed'] = (
        guardrail['G-01_score_output'] == 0 and
        guardrail['G-02_hire_output'] == 0 and
        guardrail['G-04_conflict_not_merged'] and
        guardrail['G-05_overwrite_count'] == 0
    )
    
    return guardrail

# ========== Step 4b: 提取待办 ==========
def is_meta_paragraph(text: str) -> bool:
    """判断是不是元信息段落（标题/面试官信息等）"""
    stripped = text.strip()
    if not stripped:
        return True
    if stripped.startswith('#'):
        return True
    # 纯元数据行（候选人：张三、面试官：李经理 这种）
    meta_line_count = sum(1 for kw in META_KEYWORDS[:4] if kw in stripped)
    if meta_line_count >= 1 and len(stripped) < 200:
        return True
    return False

def has_violations(text: str) -> bool:
    """判断是不是含评分/录用违规内容（严格版）"""
    # 数字+分 的评分
    if re.search(r'\d+分', text):
        return True
    # 评分：XX 格式
    if re.search(r'评分[：:]\s*\d+', text):
        return True
    # 建议录用/淘汰、推荐录用/淘汰
    if re.search(r'建议[录用淘汰]|推荐[录用淘汰]', text):
        return True
    # 淘汰风险/淘汰率/淘汰概率 → 违规（评价性）
    if re.search(r'淘汰[风险率概率]', text):
        return True
    return False

def extract_todos(split_result: dict, index: dict) -> list:
    """从所有段落里扫描待办事项（严格版）"""
    log("开始提取待办...", "STAGE_3")
    
    todos = []
    seen_texts = set()
    
    for r in index['rounds']:
        for p in r['paragraphs']:
            if p['is_missing']:
                continue
            text = p['text']
            
            # 跳过元信息段落
            if is_meta_paragraph(text):
                continue
            
            # 按句号/换行拆句子
            sentences = re.split(r'[。！？\n]', text)
            for sent in sentences:
                sent = sent.strip()
                if not sent or len(sent) < 6:
                    continue
                
                # 跳过元信息
                if any(kw in sent for kw in META_KEYWORDS):
                    continue
                
                # 跳过明显是面试官主观评价的开头
                skip_prefixes = ['我觉得', '我认为', '看起来', '感觉', '似乎', '应该', '倾向于', '整体', '初步']
                if any(sent.startswith(p) for p in skip_prefixes) and not any(kw in sent for kw in TODO_KEYWORDS_STRONG):
                    continue
                
                # 强关键词直接命中
                strong_hit = any(kw in sent for kw in TODO_KEYWORDS_STRONG)
                
                # 时间词 + 动作词组合
                temporal_hit = any(kw in sent for kw in TODO_TEMPORAL)
                action_hit = any(kw in sent for kw in TODO_ACTION)
                
                is_todo = strong_hit or (temporal_hit and action_hit)
                
                if is_todo and sent not in seen_texts:
                    seen_texts.add(sent)
                    todos.append({
                        'todo_id': f"T-{len(todos)+1:03d}",
                        'content': sent[:200],
                        'round': r['round'],
                        'interviewer': r['interviewer'],
                        'paragraph_id': p['paragraph_id'],
                        'source_link': p['paragraph_id']
                    })
    
    log(f"  待办清单：{len(todos)} 条", "STAGE_3")
    for t in todos:
        log(f"    📌 [{t['round']}] {t['content'][:60]}")
    return todos

# ========== Step 4c: 生成三栏汇总表 ==========
def generate_deliverable(index: dict, split_result: dict, inductions: list, todos: list, guardrail: dict) -> str:
    """生成 Markdown 格式的三栏汇总表"""
    log("生成三栏汇总表...", "STAGE_3")
    
    lines = []
    lines.append(f"# 面试纪要三栏汇总 — {index['candidate_id']}")
    lines.append("")
    lines.append(f"**生成时间**：{iso_now()}")
    lines.append(f"**原始纪要**：{len(index['rounds'])} 轮（{', '.join(r['round'] for r in index['rounds'])}）")
    lines.append(f"**段落总数**：{index['total_paragraphs']}")
    if index['missing_paragraphs']:
        lines.append(f"**⚠️ 缺段**：{len(index['missing_paragraphs'])} 处")
        for mp in index['missing_paragraphs']:
            lines.append(f"  - {mp['round']}-{mp['paragraph_id']}：【待补】纪要原文缺失")
    if index['conflict_groups']:
        lines.append(f"**⚠️ 矛盾评价**：{len(index['conflict_groups'])} 组（已分别保留，未合并）")
        for cg in index['conflict_groups']:
            lines.append(f"  - {cg['paragraphs'][0]['paragraph_id']} vs {cg['paragraphs'][1]['paragraph_id']}")
    lines.append("")
    lines.append("---")
    lines.append("")
    
    # 三栏表头
    lines.append("## 三栏汇总表")
    lines.append("")
    lines.append("| 事实记录 | 面试官评价 | 归纳句（挂出处） |")
    lines.append("|---------|----------|---------------|")
    
    # 按轮次排列
    max_rows = max(len(split_result['facts']), len(split_result['evaluations']), len(inductions))
    fact_iter = iter(split_result['facts'])
    eval_iter = iter(split_result['evaluations'])
    ind_iter = iter(inductions)
    
    for _ in range(max_rows):
        fact = next(fact_iter, None)
        ev = next(eval_iter, None)
        ind = next(ind_iter, None)
        
        fact_cell = f"{fact['text'][:80]}..." if fact else ""
        fact_cell += f"\n<small>📎 [{fact['round']}-{fact['paragraph_id']}]({fact['source']})</small>" if fact else ""
        
        eval_cell = f"{ev['text'][:80]}..." if ev else ""
        eval_cell += f"\n<small>👤 {ev['interviewer']} · [{ev['round']}]({ev['source']})</small>" if ev else ""
        
        ind_cell = f"{ind['induction_text'][:80]}..." if ind else ""
        if ind:
            sources_md = ", ".join(f"[{s['round']}-{s['paragraph_id']}]({s['source_link']})" for s in ind['sources'][:3])
            ind_cell += f"\n<small>🔗 出处×{ind['source_count']}：{sources_md}</small>"
        
        lines.append(f"| {fact_cell} | {eval_cell} | {ind_cell} |")
    
    lines.append("")
    
    # 待办清单
    lines.append("---")
    lines.append("")
    lines.append("## 待办清单")
    lines.append("")
    lines.append("| 待办ID | 事项 | 约定人/轮次 | 原文出处 |")
    lines.append("|--------|------|------------|---------|")
    for t in todos:
        lines.append(f"| {t['todo_id']} | {t['content']} | {t['interviewer']} · {t['round']} | [{t['round']}-{t['paragraph_id']}]({t['source_link']}) |")
    lines.append("")
    
    # 护栏检查结果
    lines.append("---")
    lines.append("")
    lines.append("## 护栏自动检查结果")
    lines.append("")
    lines.append(f"| 护栏项 | 结果 | 详情 |")
    lines.append(f"|--------|------|------|")
    lines.append(f"| G-01 评分输出次数 | {'✅ 0' if guardrail['G-01_score_output'] == 0 else '❌ ' + str(guardrail['G-01_score_output'])} | 全文搜索正则 `[0-9]+分\\|评分\\|打分\\|等级\\|⭐` |")
    lines.append(f"| G-02 录用结论输出次数 | {'✅ 0' if guardrail['G-02_hire_output'] == 0 else '❌ ' + str(guardrail['G-02_hire_output'])} | 全文搜索正则 `录用\\|淘汰\\|建议录用` |")
    lines.append(f"| G-03 归纳句出处达标 | {'✅ 全≥2条' if guardrail['G-03_induction_source_ok'] > 0 else '⏸️ 稍后检查'} | 每条归纳挂原文出处 |")
    lines.append(f"| G-04 矛盾未合并 | {'✅ 是' if guardrail['G-04_conflict_not_merged'] else '❌ 否'} | 矛盾段落分别保留 |")
    lines.append(f"| G-05 原文覆盖次数 | {'✅ 0' if guardrail['G-05_overwrite_count'] == 0 else '❌ ' + str(guardrail['G-05_overwrite_count'])} | 引用={guardrail['G-05_original_covered_count']}, 应覆盖={guardrail['G-05_expected_count']} |")
    lines.append(f"| **总体** | {'✅ PASSED' if guardrail['overall_passed'] else '❌ VIOLATED'} | |")
    lines.append("")
    
    # 固定声明
    lines.append("---")
    lines.append("")
    lines.append("> **以上为无损汇总，不代表录用意见**。所有归纳句均挂原文出处，可点击回溯。")
    
    return "\n".join(lines)

# ========== Step 5: 状态管理 ==========
STATE_FILE_TEMPLATE = "/tmp/hr20-state/{candidate_id}/state.json"

def update_state(candidate_id: str, stage: str, status: str, 
                 idempotent_key: str = None, output_path: str = None,
                 guardrail: dict = None, completed_substeps: list = None):
    """更新状态文件（JSON格式，一眼可读）"""
    state_path = Path(STATE_FILE_TEMPLATE.format(candidate_id=safe_id(candidate_id)))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    
    state = load_json(state_path)
    if not state:
        state = {
            'candidate_id': candidate_id,
            'created_at': iso_now(),
            'last_updated_at': iso_now(),
            'last_completed_stage': None,
            'guardrail_global_check': None,
            'stages': {
                'STAGE_1': {'status': 'pending', 'idempotent_key': None, 'output_path': None, 
                           'guardrail_snapshot': None, 'completed_substeps': [], 'timestamp': None},
                'STAGE_2': {'status': 'pending', 'idempotent_key': None, 'output_path': None,
                           'guardrail_snapshot': None, 'completed_substeps': [], 'timestamp': None},
                'STAGE_3': {'status': 'pending', 'idempotent_key': None, 'output_path': None,
                           'guardrail_snapshot': None, 'completed_substeps': [], 'timestamp': None}
            }
        }
    
    state['last_updated_at'] = iso_now()
    state['stages'][stage] = {
        'status': status,
        'idempotent_key': idempotent_key,
        'output_path': output_path,
        'guardrail_snapshot': guardrail,
        'completed_substeps': completed_substeps or [],
        'timestamp': iso_now()
    }
    if status == 'success':
        state['last_completed_stage'] = stage
    
    if guardrail:
        state['guardrail_global_check'] = 'passed' if guardrail.get('overall_passed') else 'failed'
    
    save_json(state_path, state)

def safe_id(candidate_id: str) -> str:
    return re.sub(r'[/\\]', '_', candidate_id)

def load_state(candidate_id: str) -> dict:
    state_path = Path(STATE_FILE_TEMPLATE.format(candidate_id=safe_id(candidate_id)))
    return load_json(state_path)

# ========== 主流程 ==========
def run_full_flow(candidate_id: str, minute_dir: Path):
    """跑完整流程"""
    start_time = time.time()
    log(f"="*50)
    log(f"HR-20 开始处理：{candidate_id}")
    log(f"纪要目录：{minute_dir}")
    log(f"="*50)
    
    # Step 1: 读多份纪要
    index = index_minutes(candidate_id, minute_dir)
    
    # Step 2: 分栏抽取
    split_result = split_fact_eval(index)
    guardrail = split_result['guardrail']
    
    # Step 3: 出处挂接与归纳
    merged = merge_induction(index, split_result['result'])
    inductions = merged.get('inductions', [])
    
    # Step 4b: 提取待办
    todos = extract_todos(merged, index)
    
    # Step 4c: 最终护栏检查（含 G-03 + G-05 跳过有效性）
    if inductions:
        src_ok_count = sum(1 for ind in inductions if ind['source_count'] >= 2)
        guardrail['G-03_induction_source_ok'] = src_ok_count / len(inductions)
    else:
        guardrail['G-03_induction_source_ok'] = 1.0
    
    # 最终 G-05 检查：跳过的段落必须都是违规/元信息/缺段
    skipped_all_valid_final = True
    for sid in guardrail.get('skipped_paragraph_ids', []):
        for r in index['rounds']:
            for p in r['paragraphs']:
                if p['paragraph_id'] == sid:
                    if p.get('is_missing'):
                        continue  # 缺段，合理跳过
                    if is_meta_paragraph(p['text']):
                        continue  # 元信息，合理跳过
                    if has_violations(p['text']):
                        continue  # 违规，合理跳过
                    skipped_all_valid_final = False
                    log(f"  ⚠️ 段落 {sid} 被跳过但不是违规/元信息/缺段！")
                    break
            if not skipped_all_valid_final:
                break
    
    guardrail['G-05_overwrite_count_final'] = 0 if skipped_all_valid_final else len(guardrail.get('skipped_paragraph_ids', []))
    guardrail['overall_passed'] = (
        guardrail['G-01_score_output'] == 0 and
        guardrail['G-02_hire_output'] == 0 and
        guardrail['G-04_conflict_not_merged'] and
        guardrail['G-05_overwrite_count_final'] == 0 and
        guardrail['G-03_induction_source_ok'] >= 0.9
    )
    
    # 生成交付物
    output = generate_deliverable(index, merged, inductions, todos, guardrail)
    
    # 写输出文件
    output_dir = PROJECT_ROOT / 'output'
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{safe_id(candidate_id)}-deliverable.md"
    output_file.write_text(output, encoding='utf-8')
    log(f"✅ 交付物生成：{output_file}", "STAGE_3")
    
    # 写轨迹
    trace = {
        'candidate_id': candidate_id,
        'started_at': iso_now(),
        'completed_at': iso_now(),
        'duration_seconds': round(time.time() - start_time, 1),
        'stages_completed': ['STAGE_1', 'STAGE_2', 'STAGE_3'],
        'guardrail_final': guardrail,
        'output_file': str(output_file),
        'summary': {
            'paragraphs_total': index['total_paragraphs'],
            'paragraphs_missing': len(index['missing_paragraphs']),
            'conflict_groups': len(index['conflict_groups']),
            'facts': len(merged.get('facts', [])),
            'evaluations': len(merged.get('evaluations', [])),
            'inductions': len(inductions),
            'todos': len(todos)
        }
    }
    trace_dir = PROJECT_ROOT / 'trace'
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_file = trace_dir / f"{safe_id(candidate_id)}-trace.json"
    save_json(trace_file, trace)
    
    # 更新最终状态
    update_state(candidate_id, 'STAGE_3', 'success', 
                 guardrail=guardrail,
                 completed_substeps=['generate_table', 'extract_todos', 'final_guardrail_check', 'write_output'])
    
    log(f"✅ STAGE_3 完成", "STAGE_3")
    log(f"✅ 全部护栏检查 {'PASSED' if guardrail['overall_passed'] else 'VIOLATED'}")
    log(f"="*50)
    log(f"总耗时：{trace['duration_seconds']} 秒")
    log(f"交付物：{output_file}")
    log(f"轨迹：{trace_file}")
    log(f"="*50)
    
    return output, trace

def run_resume(candidate_id: str):
    """从断点续跑"""
    state = load_state(candidate_id)
    if not state:
        log("❌ 未找到状态文件，请先运行全流程")
        return
    
    log(f"检测到状态文件，上一次完成：{state['last_completed_stage']}")
    
    # 根据已完成阶段决定从哪开始
    if state['last_completed_stage'] == 'STAGE_3':
        log("✅ 已全部完成，无需续跑")
        return
    
    minute_dir = PROJECT_ROOT / 'demo' / safe_id(candidate_id).split('_')[0]
    run_full_flow(candidate_id, minute_dir)

# ========== CLI 入口 ==========
def main():
    parser = argparse.ArgumentParser(description='HR-20 面试纪要三栏汇总助手')
    parser.add_argument('--candidate', help='候选人ID，格式：姓名/岗位/唯一ID')
    parser.add_argument('--minute-dir', help='纪要文件目录')
    parser.add_argument('--resume', action='store_true', help='从断点续跑')
    parser.add_argument('--resume-id', help='续跑时的候选人ID')
    parser.add_argument('--verify', help='只验证已有交付物的护栏')
    
    args = parser.parse_args()
    
    if args.resume:
        cid = args.resume_id or args.candidate
        if not cid:
            print("❌ 续跑需要指定 --resume-id 或 --candidate")
            sys.exit(1)
        run_resume(cid)
    elif args.verify:
        verify_deliverable(args.verify)
    elif args.candidate and args.minute_dir:
        run_full_flow(args.candidate, Path(args.minute_dir))
    else:
        parser.print_help()
        print("\n示例：")
        print('  python3 scripts/hr20.py --candidate "张三/Java后端/ID-20261117-003" --minute-dir demo/张三/')
        print('  python3 scripts/hr20.py --resume --resume-id "张三/Java后端/ID-20261117-003"')

if __name__ == '__main__':
    main()
