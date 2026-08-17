-- Run this in Supabase SQL Editor (one time)

CREATE OR REPLACE FUNCTION get_creativos_estilos()
RETURNS json
LANGUAGE sql
STABLE
AS $$
WITH color_agg AS (
  SELECT
    estilo_id,
    COALESCE(color, 'SIN COLOR') AS color,
    SUM(COALESCE(terex1, 0))::int AS t1,
    SUM(COALESCE(terex2, 0))::int AS t2
  FROM inventario1
  GROUP BY estilo_id, color
),
color_json AS (
  SELECT
    estilo_id,
    json_agg(
      json_build_object('color', color, 't1', t1, 't2', t2, 'total', t1 + t2)
      ORDER BY (t1 + t2) DESC
    ) AS colors,
    SUM(t1)::int AS t1,
    SUM(t2)::int AS t2,
    COUNT(*)::int AS num_colors
  FROM color_agg
  GROUP BY estilo_id
)
SELECT json_agg(
  json_build_object(
    'id',         e.id,
    'nombre',     e.nombre,
    'proveedor',  COALESCE(e.proveedor, ''),
    't1',         COALESCE(c.t1, 0),
    't2',         COALESCE(c.t2, 0),
    'total',      COALESCE(c.t1, 0) + COALESCE(c.t2, 0),
    'num_colors', COALESCE(c.num_colors, 0),
    'colors',     COALESCE(c.colors, '[]'::json)
  )
  ORDER BY e.nombre
)
FROM inventario_estilos e
LEFT JOIN color_json c ON c.estilo_id = e.id;
$$;


-- Optional: ventas aggregation (also fast)
CREATE OR REPLACE FUNCTION get_ventas_por_estilo(dias int DEFAULT 30)
RETURNS json
LANGUAGE sql
STABLE
AS $$
WITH bc AS (
  SELECT barcode, estilo_id FROM inventario1 WHERE barcode IS NOT NULL
),
vtas AS (
  SELECT barcode, COALESCE(qty, 1) AS qty
  FROM ventas_terex1
  WHERE fecha >= (CURRENT_DATE - (dias || ' days')::interval)::date
  UNION ALL
  SELECT barcode, COALESCE(qty, 1)
  FROM ventas_terex2
  WHERE fecha >= (CURRENT_DATE - (dias || ' days')::interval)::date
)
SELECT json_object_agg(estilo_id::text, total_qty)
FROM (
  SELECT bc.estilo_id, SUM(v.qty)::int AS total_qty
  FROM vtas v
  JOIN bc ON bc.barcode = v.barcode
  GROUP BY bc.estilo_id
) t;
$$;
