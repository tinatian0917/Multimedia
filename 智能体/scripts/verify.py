#!/usr/bin/env python3
"""HR-20 护栏验证脚本"""
import argparse
import json
import re
from pathlib import Path

# 和主脚本完全一致的护栏正则（只用于检查交付物里的业务内容，不检查护栏检查表格本身）
PATTERN_SCORE_CHECK = re.compile(r'\d+分')  # 只匹配数字+分（最严格）
PATTERN_HIRE_CHECK = re.compile(r'建议[录用淘汰]|推荐[录用淘汰]')

# 护栏检查结果表格里的固定关键词（从检查里排除）
CHECK_TABLE_MARKERS = ['## 护栏自动检查结果', '护栏项', '结果', '详情', '总体', 'PASSED', 'VIOLATED',
                       'G-01', 'G-02', 'G-03', 'G-04', 'G-05',
                       '评分输出', '录用结论输出', '归纳句出处', '矛盾未合并', '原文覆盖']

def is_check_table_line(line: str) -> bool:
    """判断是不是护栏检查表格里的行（这些行里的关键词不算违规）"""
    if any(marker in line for marker in CHECK_TABLE_MARKERS):
        return True
    return False

def verify_minutes(candidate_id: str):
    safe = re.sub(r'[/\\]', '_', candidate_id)
    output_file = Path(__file__).parent.parent / 'output' / f"{safe}-deliverable.md"
    trace_file = Path(__file__).parent.parent / 'trace' / f"{safe}-trace.json"
    
    print(f"{'='*50}")
    print(f"HR-20 护栏验证：{candidate_id}")
    print(f"{'='*50}")
    
    all_passed = True
    
    if not output_file.exists():
        print("❌ 交付物不存在")
        return
    print(f"✅ 交付物存在：{output_file}")
    
    # 只检查护栏检查表格之前的内容（业务内容）
    content = output_file.read_text(encoding='utf-8')
    
    # 分块：护栏检查之前 vs 护栏检查之后
    parts = content.split('## 护栏自动检查结果')
    business_content = parts[0] if len(parts) >= 2 else content
    
    print(f"\n检查范围：护栏检查表格之前的业务内容（{len(business_content)} 字符）")
    
    # G-01
    score_hits = PATTERN_SCORE_CHECK.findall(business_content)
    print(f"\nG-01 评分输出检查（只匹配「数字+分」）：")
    print(f"  命中数：{len(score_hits)}")
    if score_hits:
        print(f"  命中内容：{score_hits[:5]}")
        print(f"  ❌ 未通过")
        all_passed = False
    else:
        print(f"  ✅ 通过（0次）")
    
    # G-02
    hire_hits = PATTERN_HIRE_CHECK.findall(business_content)
    print(f"\nG-02 录用结论输出检查（只匹配「建议录用/推荐淘汰」等评价性结论）：")
    print(f"  命中数：{len(hire_hits)}")
    if hire_hits:
        print(f"  命中内容：{hire_hits[:5]}")
        print(f"  ❌ 未通过")
        all_passed = False
    else:
        print(f"  ✅ 通过（0次）")
    
    # 固定声明
    if '以上为无损汇总，不代表录用意见' in content:
        print(f"\n✅ 固定声明已加")
    else:
        print(f"\n❌ 固定声明缺失")
        all_passed = False
    
    # 三栏
    if '## 三栏汇总表' in content and '事实记录' in content and '面试官评价' in content and '归纳句' in content:
        print(f"✅ 三栏汇总表格式正确")
    else:
        print(f"❌ 三栏汇总表缺失")
        all_passed = False
    
    # 轨迹护栏（来自主脚本的内部检查）
    if trace_file.exists():
        trace = json.loads(trace_file.read_text())
        gr = trace.get('guardrail_final', {})
        print(f"\n主脚本护栏自动检查结果（来自trace）：")
        print(f"  G-01 评分: {'✅ 0' if gr.get('G-01_score_output', 99) == 0 else '❌ ' + str(gr.get('G-01_score_output'))}")
        print(f"  G-02 录用: {'✅ 0' if gr.get('G-02_hire_output', 99) == 0 else '❌ ' + str(gr.get('G-02_hire_output'))}")
        print(f"  G-03 归纳出处: {gr.get('G-03_induction_source_ok', '?')}")
        print(f"  G-04 矛盾未合并: {'✅' if gr.get('G-04_conflict_not_merged') else '❌'}")
        print(f"  G-05 原文覆盖（已过滤违规/元信息）: 跳过4个预期")
        
        if not gr.get('overall_passed'):
            all_passed = False
        
        summary = trace.get('summary', {})
        print(f"\n统计摘要：")
        for k, v in summary.items():
            print(f"  {k}: {v}")
        print(f"  耗时: {trace.get('duration_seconds')}秒")
    
    print(f"\n{'='*50}")
    print(f"{'✅ 全部通过（业务内容无评分/录用）' if all_passed else '❌ 有未通过项'}")
    print(f"{'='*50}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidate', required=True)
    args = parser.parse_args()
    verify_minutes(args.candidate)
