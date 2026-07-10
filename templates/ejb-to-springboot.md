# 模板:WildFly (Java 6/8, EJB) → Spring Boot 3.5 / Java 17 搬移

> 在 loop 外的 session 使用:把 `<>` 佔位符換成實際內容,盤點表逐列填滿(證據附 檔:行),
> 產出 goal.md + plan.json(任務清單)+ 分析文件(本模板展開後的內容,commit 進 repo 供 ref)交人審。
> validate cmd 建議:`mvn -q test`。

## 0. Goal(同步產出 goal.md)

把 `<舊專案路徑>` 的 `<範圍,例:全部 EJB 與對外 API>` 搬到本專案,邏輯等價。
粗略即可:API 全搬、行為等價、`mvn test` 綠。不追加效能優化、不重新設計架構。

## 1. 盤點表(沒列到=不存在)

### 1a. EJB 元件

| 元件 | 類型(@Stateless/@Stateful/@Singleton/MDB) | 檔:行 | 交易屬性(見 §2) | 目標(Spring Bean) |
|---|---|---|---|---|
| `<XxxServiceBean>` | | | | |

### 1b. 容器服務

- JNDI lookup 清單(檔:行 → 改成注入什麼)
- `ejb-jar.xml` / `jboss-*.xml` descriptor 覆寫清單(逐條:覆寫了什麼、對應 Spring 設定)
- `@Schedule` timer 清單 —— **注意:persistent timer 在 @Scheduled 沒有等價物**,
  需要跨重啟持久化的逐個標記,決策:Quartz/ShedLock 或接受不持久(human gate)
- Interceptor 清單 → Spring AOP 對應
- `@RolesAllowed` / security domain → Spring Security 對應
- JAX-WS / JAX-RS 端點清單 → Spring 對應
- JMS 目的地與 MDB → @JmsListener 對應

## 2. Transaction 專章(最深的坑,逐方法盤點)

**鐵律:EJB CMT 下所有 business method 沒標註也是 `REQUIRED`;Spring 沒標 `@Transactional`
就是無交易(每條 SQL autocommit)。** 所以:

| Bean.method | 有效交易屬性(EJB) | 目標 @Transactional | rollback 特例 | 釘行為的測試 |
|---|---|---|---|---|
| `<Xxx.doIt>` | REQUIRED(預設) | `@Transactional` | | `<XxxTxTest#...>` |

逐項檢查:

- `@ApplicationException(rollback=true)` 的 checked exception → Spring 端必須補 `rollbackFor`,
  漏掉 = 資料不一致等級 bug。
- `REQUIRES_NEW` / `NOT_SUPPORTED` / `MANDATORY` / `NEVER` 逐個列,不准默認。
- BMT(`UserTransaction`)→ `TransactionTemplate` 手動改寫。
- **XA 判定**:舊系統有 DB+JMS 同交易(WildFly JTA)的話,Spring Boot 預設沒有 2PC——
  引 Narayana/Atomikos 或改設計,這是 human gate,規劃書裡標出來交人裁決。
- 交易 timeout 設定(descriptor/annotation)逐條搬。
- **每個非預設交易屬性 → 一條 Testcontainers 整合測試釘死行為**
  (丟例外後資料到底有沒有留下來)。

## 3. 平台跳版清單

- `javax.*` → `jakarta.*`(Spring Boot 3 硬要求)。
- JDK 移除模組:JAXB / activation / corba / JAX-WS 要補依賴。
- 舊 Hibernate → 6.x:查詢行為、lazy loading、dialect 差異逐項驗。
- 移除的 API(sun.misc 等)、反射存取限制。

## 4. 等價驗證方法(characterization test)

先照**舊碼**行為寫測試(規格從舊碼讀出來,證據附 檔:行),新碼必須過同一套。
交易行為一律 Testcontainers + 真 DB 釘,不用 mock 騙自己。

## 5. 任務化(loop 規劃期會轉成 plan JSON 並補漏)

- T01: 建 Testcontainers 測試骨架 + CI 綠(後續任務的證據都放這)
- T02: 搬 `<模組A>`(盤點表 §1a 第 n 列,含 §2 對應交易測試)
- ...(粒度:一輪做得完;依賴排序;每條寫 DoD)

## 6. 完整性 gate

盤點表(§1a/§1b/§2)每一列對得到「已完成任務 + 測試(檔:行)」;`mvn test` 綠。
對不上的列 = 沒搬完。

## 7. 人工驗收(不進迴圈)

- XA/持久 timer 等 human gate 決策項
- 效能/監控/部署設定
