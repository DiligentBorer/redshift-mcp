-- 声明式 SQL 工具 active_devices_by_day 的查询（被 conf.d/sql_tools.example.yaml 经 sql_file 引用）。
-- 命名占位符：%(date)s、%(country)s。
-- LIMIT 可写可不写：顶层缺 LIMIT 时 server 会自动追加 LIMIT (max_rows+1) 下推到 DB；
-- 显式写了（如下面的 LIMIT 100）则原样尊重、不被收紧。这里保留 100 演示「显式写则尊重」。
SELECT country, COUNT(DISTINCT device_id) AS devices
FROM analytics.events
WHERE event_date = %(date)s
  AND country = %(country)s
GROUP BY country
LIMIT 100
