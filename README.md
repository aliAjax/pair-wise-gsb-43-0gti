# 公共采购密封投标与评审系统

标准库实现的资格预审、招标发布、密封投标、开标校验收、规则评分、利益冲突、澄清、废标、投诉重评和授标快照服务。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8209`，数据库默认 `public_procurement.db`。

## 主要接口

使用 `X-User`、`X-Role` 请求头。角色有 `procurement`、`vendor`、`evaluator`、`supervisor`、`auditor`、`public`。

- `GET /health`、`GET /api/state`、`GET /api/tenders/{id}`
- `POST /api/vendors`、`POST /api/tenders`、`POST /api/tenders/publish`
- `POST /api/qualifications`：开标前供应商提交或补交资格材料
- `POST /api/qualifications/review`：采购方逐份审核，开标前可改结论
- `POST /api/bids`、`POST /api/bids/withdraw`、`POST /api/bids/disqualify`
- `POST /api/tenders/open`：截止后开标并核验承诺哈希
- `POST /api/conflicts`、`POST /api/evaluations`
- `POST /api/clarifications`、`POST /api/clarifications/answer`
- `POST /api/complaints`、`POST /api/complaints/resolve`
- `POST /api/tenders/award`：锁定评分轮次并保存排名快照

## 资格预审

资格判断在独立模块 `qualification.py`。供应商在开标前提交资格材料，采购方逐份审核；只有审核通过的供应商才能投标，未提交、待审核或未通过的投标都会被挡下。开标后资格结果冻结：材料不能补交，审核不能更改，已通过的供应商不受影响。页面和 `/api/state`、`/api/tenders/{id}` 会列出审核状态。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整开标授标、截止前正文隐藏、利益冲突、重复评分覆盖、投诉重评、资格预审门禁与开标冻结和角色权限。

## 局限

供应商与请求用户没有绑定校验，身份仍依赖请求头；投标正文虽然按接口阶段隐藏，但数据库本身未加密；评分规则适合演示，资格预审仅覆盖材料提交与审核，不覆盖保证金、电子签名和采购法规差异。
