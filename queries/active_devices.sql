-- 声明式 SQL 工具 active_devices_by_day 的查询（被 conf.d/sql_tools.example.yaml 经 sql_file 引用）。
-- 命名占位符：%(date)s、%(country)s。务必自带 LIMIT（声明式工具的安全闸门不自动加 LIMIT）。
SELECT country, COUNT(DISTINCT device_id) AS devices
FROM analytics.events
WHERE event_date = %(date)s
  AND country = %(country)s
GROUP BY country
LIMIT 100
