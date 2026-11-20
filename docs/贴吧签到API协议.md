# 百度贴吧签到 API 协议文档(三档接口)

> **文档目的**: 供 AI 助手/自动化程序直接复用的贴吧签到协议完整规格
> **实测日期**: 2026-11-17(全部结论经真实账号逐项验证, 非二手资料)
> **验证环境**: Python 3.11 纯标准库(urllib), Windows + Linux(CentOS)
> **文档性质**: 技术知识, 无隐私数据, 可自由传播

## 0. 核心结论(先读这个)

1. **经验加成由"接口路径"决定, 与 User-Agent 无关**
   (实测: 手机 UA 调网页接口, 经验仍按网页档 +2 计)
2. 三档接口中 **客户端档经验最高(+6/+8), 且主域路径 IPv6 可达**,
   是自动化首选方案
3. 手机网页档(`mo/q/sign`)在 2026-11 实测已被百度收紧(11000), 不可用
4. 客户端协议的三大坑: `itb_tbs` vs 网页 `tbs` 是两种 token;
   签名是 `key=value` 拼接; BDUSS 要放 body+Cookie 双份

| 档位 | 接口 | 首签经验 | 连续经验 | 2026-11 现状 |
|------|------|---------|---------|-------------|
| 电脑网页 | `tieba.baidu.com/sign/add` | +2 | +4 | ✅ 可用 |
| 手机网页 | `tieba.baidu.com/mo/q/sign` | +3 | +5 | ❌ 已失效(11000) |
| 客户端 | `tieba.baidu.com/c/c/forum/sign` | **+6** | **+8** | ✅ 可用(推荐) |

## 1. 通用准备

### 1.1 凭证(BDUSS)

- 唯一必需凭证: `BDUSS`(浏览器登录百度后的 Cookie)
- 获取: 浏览器登录 tieba.baidu.com → F12 → Application → Cookies →
  复制 BDUSS 值(约 192 字符)
- `STOKEN` 非签到必需(仅网页版部分接口用)
- **失效条件**: 仅"修改密码"或"账号设置里退出所有设备"会导致失效;
  其他设备扫码/登录均共存共存, 不会踢掉旧 BDUSS

### 1.2 一站式信息接口 newmoindex(强烈推荐)

```
GET https://tieba.baidu.com/mo/q/newmoindex
Cookie: BDUSS=<bduss>
User-Agent: 任意
```

一次请求返回全部所需信息:

```json
{
  "data": {
    "uid": 1234567890,
    "tbs": "16位网页版tbs",
    "itb_tbs": "24位客户端用tbs",
    "like_forum": [
      {
        "forum_name": "吧名",
        "forum_id": 123456,
        "user_level": "3",
        "user_exp": "15",
        "is_like": true,
        "is_sign": 0
      }
    ]
  }
}
```

| 字段 | 用途 |
|------|------|
| `uid` | 空/缺失 = BDUSS 失效(登录态检测) |
| `itb_tbs` | **客户端档签到必需**(24位) |
| `tbs` | 网页档签到用(16位) — 两者不可混用! |
| `forum_id` | 客户端档的 `fid` 参数 |
| `is_sign` | 1=今日已签, 0=未签(签到前过滤, 省请求) |
| `is_like` | false=关注但未加入会员, 签到必失败, 跳过 |
| `user_exp` | 当前吧经验值(可用于经验增量验证) |

### 1.3 签到重置时间

每日 00:00(北京时间)重置签到状态。

## 2. 三档接口详细规格

### 2.1 电脑网页档(+2/+4)

```
POST https://tieba.baidu.com/sign/add
Content-Type: application/x-www-form-urlencoded
Cookie: BDUSS=<bduss>
User-Agent: 任意

body: kw=<吧名>&tbs=<16位tbs>
```

**坑**: `kw` 必须按 **GBK** 编码 urlencode(不是 UTF-8!)

响应(网页版错误体系是 `no` 字段):

```json
{"no": 0, "error": "", "data": {"uinfo": {...}}}
```

| no | 含义 |
|----|------|
| 0 | 成功 |
| 1101 / 1102 | 今日已签 |
| 1011 | 未加入该吧会员 |
| 1 | tbs 错误 |

### 2.2 手机网页档(+3/+5) — 2026-11 已失效, 仅存档

历史可用形态(开源项目 Tieba-Cloud-Sign 的实现):

```
GET https://tieba.baidu.com/mo/q/sign?tbs=<tbs>&kw=<urlencode吧名>&is_like=1&fid=<fid>
Cookie: BDUSS=<bduss>; BAIDUID=<伪造的md5大写>
User-Agent: iPhone Safari
Referer: https://tieba.baidu.com/f?kw=<吧名>
```

2026-11-17 实测: 无论参数如何组合, 恒返回
`{"no":11000,"error":"汗，操作未成功，麻烦再试一下下"}`。
同时手机版 HTML 页面(`/mo/q/m?kw=`)会触发「百度安全验证」风控页
(`subid: tb_wise_pb_bfe`)。结论: 手机网页签到通道已被百度关闭,
引导用户去 App。**不要在这条路上浪费时间。**

### 2.3 客户端档(+6/+8) — 推荐方案 ⭐

```
POST https://tieba.baidu.com/c/c/forum/sign
Content-Type: application/x-www-form-urlencoded
Cookie: BDUSS=<bduss>
User-Agent: bdtb for Android 6.5.8
```

**域名双路由(重要)**:
- `tieba.baidu.com/c/c/forum/sign` — 主域, **有 IPv6(AAAA)** ✅
- `c.tieba.baidu.com/c/c/forum/sign` — 客户端子域, **纯 IPv4(无 AAAA)**
- 两域后端完全一致(实测同参数返回逐字节同构响应)
- IPv6-only 环境必须走主域; 普通 IPv4 环境两者皆可

**Body 参数(全部必填)**:

| 参数 | 值 | 说明 |
|------|-----|------|
| `BDUSS` | 凭证值 | **body 和 Cookie 里都要有** |
| `fid` | 吧的 forum_id | newmoindex 的 `forum_id` |
| `kw` | 吧名 | UTF-8 urlencode |
| `tbs` | 24位 token | **必须用 `itb_tbs`**, 见 1.2 |
| `_client_version` | `12.22.1.0` | 实测可用版本号 |
| `_client_type` | `4` | 实测可用 |
| `sign` | 签名 | 见下方算法 |

**签名算法(逐步)**:

```python
import hashlib

def client_sign(params):
    """params 为除 sign 外的全部 body 参数(dict)"""
    items = sorted(params.items())            # 1. 按 key ASCII 升序(ksort)
    raw = "".join("%s=%s" % (k, v) for k, v in items)  # 2. key=value 无分隔拼接
    raw += "tiebaclient!!!"                   # 3. 固定盐
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()  # 4. MD5大写
```

**响应判断(注意嵌套结构, 判定顺序重要)**:

```python
obj = json.loads(text)
if "user_info" in obj:                    # ① 成功(顶层可能无 error_code)
    exp = obj["user_info"]["sign_bonus_point"]   # "6"或"8" = 本次经验
elif obj.get("error_code") in ("340011", "160002"):   # ② 已签
    ...
elif (obj.get("error") or {}).get("errno") == "320022":  # ③ tbs失效
    ...  # 注意: 此响应顶层 error_code 是 "0", 别误判为成功!
```

### 2.4 错误码总表(客户端档)

| error_code / error.errno | 含义 | 处理建议 |
|--------------------------|------|---------|
| (无, 有 user_info) | 签到成功 | 记录 sign_bonus_point |
| `340011` | 已签("你签得太快了") | 跳过, 正常 |
| `160002` | 已签(重复签到) | 跳过, 正常 |
| `1011` | 未加入会员 | 永久跳过该吧 |
| `320022` | tbs 校验失败 | 重新取 itb_tbs 重试一次 |
| `110001` | 签名/参数错误 | 检查签名算法与参数完整性 |

## 3. 网络可达性(IPv6 部署视角)

实测 DNS(移动 DNS 211.136.17.107)与连通性(2026-11):

| 域名 | AAAA | 说明 |
|------|------|------|
| `tieba.baidu.com` | ✅ 有 | 网页/客户端主域/接口全覆盖 |
| `c.tieba.baidu.com` | ❌ 无 | 纯 IPv4 |
| `tieba.baidu.com` 路径 `/c/c/*` | ✅ | **主域路由客户端API, 这是关键** |

经验: 国内 IPv6 出口普遍可达 tieba 主域; 国际 IPv6 未必。
若部署环境为 IPv6-only(如部分国内云服务器), 走主域即可全功能运行。

## 4. 最小可用实现(Python 纯标准库)

```python
#!/usr/bin/env python3
# 贴吧客户端档签到最小实现(完整流程, 可直接运行)
import hashlib
import json
import urllib.parse
import urllib.request

BDUSS = "你的BDUSS"   # 浏览器 F12 复制
UA = "bdtb for Android 6.5.8"


def moindex():
    """一次拿: 登录态 + itb_tbs + 关注列表(含fid/已签状态)"""
    req = urllib.request.Request(
        "https://tieba.baidu.com/mo/q/newmoindex",
        headers={"User-Agent": UA, "Cookie": "BDUSS=" + BDUSS})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode())["data"]
    if not data.get("uid"):
        raise RuntimeError("BDUSS 已失效")
    return data["itb_tbs"], [
        (it["forum_name"], it["forum_id"])
        for it in data["like_forum"]
        if not it.get("is_sign") and it.get("is_like")
    ]


def client_sign(params):
    items = sorted(params.items())
    raw = "".join("%s=%s" % (k, v) for k, v in items) + "tiebaclient!!!"
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def sign(kw, fid, tbs):
    params = {
        "BDUSS": BDUSS, "fid": str(fid), "kw": kw, "tbs": tbs,
        "_client_version": "12.22.1.0", "_client_type": "4",
    }
    params["sign"] = client_sign(params)
    req = urllib.request.Request(
        "https://tieba.baidu.com/c/c/forum/sign",      # 主域(IPv6 可达)
        data=urllib.parse.urlencode(params).encode(),
        headers={"User-Agent": UA, "Cookie": "BDUSS=" + BDUSS})
    with urllib.request.urlopen(req, timeout=20) as r:
        obj = json.loads(r.read().decode())
    if "user_info" in obj:
        return "ok", obj["user_info"].get("sign_bonus_point")
    ec = str(obj.get("error_code", ""))
    if ec in ("340011", "160002"):
        return "signed", ec
    return "fail", ec or (obj.get("error") or {}).get("errno", "?")


if __name__ == "__main__":
    tbs, todo = moindex()
    for kw, fid in todo:
        state, detail = sign(kw, fid, tbs)
        print("[%s] %s %s" % (kw, state, detail))
        # 间隔 2-6 秒随机, 防风控
```

## 5. 坑点清单(按踩坑顺序)

1. **tbs 混用**: 网页档要 16 位 `tbs`, 客户端档要 24 位 `itb_tbs`。
   客户端接口用网页 tbs → `320022 TBS校验失败`
2. **签名拼接**: 是 `key=value` 相连, 不是纯 value 相连。
   纯 value 签名 → `110001 未知错误`
3. **BDUSS 位置**: body + Cookie 双份(实测组合)。只放一处未验证
4. **编码**: 网页 sign/add 的 kw 是 **GBK** urlencode; 客户端接口是 UTF-8
5. **320022 响应结构**: 顶层 `error_code:"0"` 但 `error.errno:"320022"`,
   判断必须先查 `user_info` 再查嵌套 errno, 否则误判成功
6. **UA 无加成**: 换手机 UA 调网页接口不会变 +5(2026-11 实测 +2)
7. **mo 通道已死**: 别试 `mo/q/sign`, 恒 11000(见 2.2)
8. **频率控制**: 逐吧间隔 2-6 秒随机较稳; 连续高速请求有风控风险
9. **is_like=false 的吧**: 直接跳过, 签了必 1011
10. **tbs 非一次性**: 同一 tbs 可连续签多个吧(当天有效),
    失效(320022)时重取一次即可

## 6. 部署建议(来自生产环境运行实践)

- **推荐流程**: newmoindex(登录态+itb_tbs+列表) → 过滤未签 →
  逐吧客户端档签到(2-6s 随机间隔) → 失败吧重试 tbs 一次
- **登录态检测**: newmoindex 无 `uid` = BDUSS 失效, 退出码区分
  (如 2=凭证失效, 触发告警邮件通知人工换 BDUSS)
- **幂等性**: 重复运行安全(已签的吧返回 340011 跳过),
  可放心手动补跑
- **经验验证**: 签到成功响应的 `sign_bonus_point` 是权威经验值,
  写入日志可做长期统计(如周报经验汇总)
- **服务器 IPv6-only 部署**: 全程走 `tieba.baidu.com` 主域即可,
  无需任何 IPv4 依赖

## 附: 实测记录(2026-11-17)

| 实验 | 结果 |
|------|------|
| 网页 sign/add + 电脑UA(bilibili吧) | +2 经验 |
| 网页 sign/add + 手机UA(deepseek吧) | +2 经验(UA无关实锤) |
| 主域客户端 + dc的tbs(华南理工大学吧) | +6, sign_bonus_point="6" |
| 主域客户端 + itb_tbs(gpt吧) | +6, 单接口方案成立 |
| 主域 vs c.子域同参数对比 | 响应逐字节同构 |
| mo/q/sign 各种参数组合 | 恒 11000(通道已关闭) |
| 服务器(IPv6-only)全链路(抗压背锅/离谱吧) | +6×2, 经验 1→7 到账 |
