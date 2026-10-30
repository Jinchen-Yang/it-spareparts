select table_name,column_name,data_type,character_maximum_length,numeric_precision,numeric_scale,is_nullable,column_default
from information_schema.columns where table_name in ('f_maintenance_order','f_maintenance_line') order by table_name,ordinal_position;
select 'orders',count(*),min(order_date),max(order_date) from f_maintenance_order;
select 'lines',count(*) from f_maintenance_line;
