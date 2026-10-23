# Room ID 重构设计：移除 ID 作为数据主键

## 背景

当前系统使用缴费系统的 `room_id`（如 `101223`）作为数据模型的核心 key：
- 原始数据目录：`database/仙林校区/1幢/1A102-101223/`
- Summary JSON key：`"101223": {...}`
- Summary 文件名：`rooms/101223.json`

问题：`room_id` 是缴费系统的动态 ID，可能被重新分配给其他房间，导致数据错乱。

## 目标

彻底移除 `room_id` 作为数据主键，改用房间名（如 `1A102`）作为主键。`room_id` 仅作为 API 查询参数，不出现在目录结构和 JSON 键名中。

本次范围：**先后端再前端**，前端迁移不在本次范围内。

## 方案：房间名为主键 + 映射文件 + 历史 ID 记录

### 1. 映射文件 `config/room_ids.json`

```json
{
  "仙林校区": {
    "1幢": {
      "1A102": {
        "current_id": "101223",
        "previous_ids": ["100492"]
      },
      "1A112": {
        "current_id": "98799",
        "previous_ids": []
      }
    }
  }
}
```

映射文件是可选的辅助工具，查询和聚合流程不依赖它也能正常工作。

### 2. 原始数据目录

```
# 变更前: database/仙林校区/1幢/1A102-100492/20260517.json
# 变更后: database/仙林校区/1幢/1A102/20260517.json
```

同名房间多个 ID 目录的数据合并（按日期去重）。

原始数据 JSON 移除 `id` 和 `宿舍ID` 字段：
```json
{
  "校区": "仙林校区",
  "楼栋": "1幢",
  "房间": "1A102",
  "剩余电量": "8.85度",
  "success": true
}
```

### 3. Summary JSON

**rooms/{room_name}.json：**
```json
{
  "room_name": "1A102",
  "campus": "仙林校区",
  "building": "1幢",
  "current_balance": 10.82,
  "balance_history": { "20260517": 8.85, ... },
  "last_updated": "20260527"
}
```

`room_id` 字段移除。

**buildings/{building}/summary.json：**
```json
{
  "building": "1幢",
  "campus": "仙林校区",
  "total_rooms": 29,
  "rooms": {
    "1A102": {
      "current_balance": 10.82,
      "last_updated": "20260527"
    }
  }
}
```

Key 从 `"101223"` 改为 `"1A102"`。

**campuses/{campus}/summary.json / overview.json：**
如有 room_id 引用则同步更新。

### 4. 查询流程（`nju_electric_query.py`）

```
输入: room_id 列表 (如 "101223 98799")  ← 与现在一致
                    ↓
       用 room_id 调用 API 查询
                    ↓
       解析返回的 校区/楼栋/房间名
                    ↓
       数据存到 {campus}/{building}/{room}/ 目录
       (JSON 不含 id/宿舍ID 字段)
```

**不读写映射文件，不关心 ID 变更。** 只负责查询和存储。

### 5. 映射文件更新机制（`scripts/update_mapping.py`）

单独脚本维护映射文件，职责与查询脚本分离：

```
1. 加载现有映射 config/room_ids.json
2. 从 room_ids.txt 或其他来源获取所有 room_id
3. 逐个查询 API，记录返回的 (campus, building, room_name)
4. 更新映射:
   - room_name 不在映射中 → 新建条目
   - room_name 存在且 current_id 一致 → 无变更
   - room_name 存在但 current_id 不同 → 旧 ID 移入 previous_ids，更新 current_id
   - ID 指向变更: 旧房间的 current_id 移入 previous_ids 并设为 null，
     新房间创建条目 current_id=该ID
5. 写入映射文件
```

### 6. 数据迁移脚本（`scripts/migrate_room_ids.py`，一次性）

```
步骤1: 扫描原始数据，建立映射表
  - 遍历 database/{campus}/{building}/{room}-{room_id}/
  - 读取最新 JSON 获取校区/楼栋/房间名
  - 记录: (campus, building, room_name) → [room_id1, room_id2, ...]
  - 最新日期的 ID 为 current_id，其余为 previous_ids

步骤2: 迁移原始数据目录
  - {room}-{room_id}/ → {room}/
  - 同名房间多个 ID 目录的数据合并（按日期去重）
  - 同一天有多个文件时，优先保留 latest ID（目录名中数字最大的 ID）对应的数据
  - JSON 中移除 id 和 宿舍ID 字段

步骤3: 迁移 summaries 数据
  a) rooms/{room_id}.json → rooms/{room_name}.json
     - 移除 room_id 字段
     - 合并同名房间的多个 summary（balance_history 合并）
  b) buildings/{building}/summary.json
     - rooms key 从 room_id 改为 room_name
     - 合并同名房间的条目
  c) campuses/{campus}/summary.json
     - buildings 中引用的房间数重新计算
  d) campuses.json / overview.json
     - 如有 room_id 引用则同步更新

步骤4: 写入 config/room_ids.json 映射文件

步骤5: 删除空目录和旧文件
```

### 7. 受影响的脚本

**需要修改：**

| 脚本 | 变更 |
|------|------|
| `nju_electric_query.py` | `save_result()` 路径改为 `{room}`；JSON 移除 `id`/`宿舍ID` 字段 |
| `aggregate_data.py` | 不再从目录名解析 ID；summary key 用 room_name |
| `generate_index.py` | 索引 key 从 room_id 改为 room_name |
| `generate_building_details.py` | 同上 |
| `extract_room_ids.py` | 从映射文件读取 |
| `query_by_name.py` / `query_by_room.py` | 通过映射文件查找 room_id |
| `rollback_failed_run.py` | 路径适配 |
| `serve_docs.py` | 路径引用适配 |

**可删除：**

| 脚本 | 原因 |
|------|------|
| `cleanup_duplicate_ids.py` | 不再有 ID 重复的目录 |
| `find_duplicate_room_ids.py` | 同上 |

**新增：**

| 文件 | 用途 |
|------|------|
| `config/room_ids.json` | 房间名→ID 映射 |
| `scripts/migrate_room_ids.py` | 一次性迁移脚本 |
| `scripts/update_mapping.py` | 映射文件更新脚本 |
