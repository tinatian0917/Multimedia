# interview-minutes-indexer 工具配置

## 工具元数据
- 工具ID: interview-minutes-indexer
- 版本: v1.0
- 权限: 只读
- 依赖: 文件系统读权限（/Users/tina/Documents/D-Work/2026劳动竞赛/HR智能系统/智能体/demo/ 及以下）

## 参数 Schema
```json
{
  "candidate_id": {
    "type": "string",
    "required": true,
    "maxLength": 50,
    "pattern": "^.+/.+/ID-.+$"
  },
  "minute_files": {
    "type": "array",
    "required": true,
    "minItems": 2,
    "maxItems": 10,
    "items": {
      "type": "object",
      "required": ["round", "interviewer", "file_path"],
      "properties": {
        "round": {"type": "string", "enum": ["R1", "R2", "R3", "R4", "R5"]},
        "interviewer": {"type": "string", "minLength": 1},
        "file_path": {"type": "string", "minLength": 1}
      }
    }
  },
  "index_version": {
    "type": "string",
    "required": false,
    "default": "v1.0"
  }
}
```

## 错误码表
| 错误码 | 含义 | 处理 |
|--------|------|------|
| E-001 | 候选人标识不合法 | 直接返回，不重试 |
| E-002 | 纪要数量不在 [2,10] 范围 | 直接返回，不重试 |
| E-003 | 某份文件路径为空或不存在 | 直接返回，不重试 |
| E-004 | 检测到重复文件路径 | 直接返回，不重试 |
| E-005 | 轮次标识重复 | 直接返回，不重试 |
| E-101 | 文件读取 I/O 错误 | 重试 2 次 |
| E-102 | 文件格式解析失败 | 重试 2 次 |
| E-999 | 超过最大重试次数 | 转人工 |

## 超时与重试
| 配置项 | 值 |
|--------|-----|
| 单次超时 | 30 秒 |
| 最大重试 | 2 次 |
| 重试间隔 | 指数退避（1s, 2s, 4s） |
| 超时重试范围 | E-101, E-102 |

## 幂等键算法
```
idempotent_key = SHA256(
  candidate_id 
  + "|" 
  + file_paths_sorted_by_round  // 按轮次排序的文件路径列表
  + "|" 
  + SHA256(file_content_concat)  // 所有文件内容拼接的 hash
  + "|" 
  + index_version
)
```
缓存有效期: 1 小时
缓存位置: /tmp/hr20-cache/{idempotent_key}

## 输出 Schema（结构化索引）
```json
{
  "candidate_id": "张三/Java后端/ID-20261117-003",
  "index_version": "v1.0",
  "total_paragraphs": 41,
  "missing_paragraphs": [{"round": "R3", "seq": 4, "label": "补充验证段落缺失"}],
  "conflict_groups": [
    {
      "type": "evaluation",
      "paragraphs": [
        {"round": "R2", "seq": 7, "text": "技术基础比较一般，深度不够"},
        {"round": "R3", "seq": 6, "text": "候选人技术扎实，逻辑清晰"}
      ]
    }
  ],
  "rounds": [
    {
      "round": "R1",
      "interviewer": "李经理",
      "paragraphs": [
        {
          "paragraph_id": "R1-P001",
          "seq": 1,
          "text": "候选人张三本科毕业5年，一直在做Java后端开发...",
          "content_hash": "sha256:a1b2c3...",
          "is_missing": false
        }
      ]
    }
  ]
}
```
