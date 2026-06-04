-- Error API IP 命中统计（演示版本）—— error_api 插件的内聚 SQL。
-- 绑定参数：event_date（事件日期）、limit（= max_rows + 1，用于服务端截断判断）。
-- 真实部署可按业务覆盖此 SQL（保持插件包同样的命名占位符）。
SELECT DISTINCT client_ip, COUNT(DISTINCT device_id) AS device_count
FROM analytics.events
WHERE event_date = %(event_date)s
  AND country = 'US'
GROUP BY client_ip
ORDER BY device_count DESC
LIMIT %(limit)s
