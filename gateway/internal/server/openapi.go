package server

import (
	"fmt"

	"github.com/hamr-hub/a-source-gateway/gateway/internal/parser"
)

// OpenAPIDocument returns the OpenAPI 3.1 description of the gateway.
//
// 注意: 这份描述是**手工维护的字面量**, 不是从上面那张路由表生成的 ——
// 改路由时必须同步改这里, 否则客户端生成器会拿到过期的描述。
func OpenAPIDocument() *parser.Object {
	doc := parser.NewObject()
	doc.Set("openapi", "3.1.0")
	doc.Set("info", infoObject())
	doc.Set("servers", []any{parser.NewObjectWith("url", "http://127.0.0.1:8848")})
	doc.Set("paths", pathsObject())
	doc.Set("components", componentsObject())
	return doc
}

func infoObject() *parser.Object {
	info := parser.NewObject()
	info.Set("title", "A-Class Source Gateway")
	info.Set("version", "1.0")
	info.Set("description", "海外 A 类(法规强制公开)数据源的通用元数据 + 实时查询网关。"+
		"不存储业务数据, 只存源元数据;所有响应带 meta 描述请求与返回。")
	return info
}

func pathsObject() *parser.Object {
	paths := parser.NewObject()

	root := parser.NewObject()
	root.Set("get", op("服务信息与路由表", nil, nil, nil))
	paths.Set("/", root)

	health := parser.NewObject()
	health.Set("get", op("存活探针", nil, nil, nil))
	paths.Set("/healthz", health)

	live := parser.NewObject()
	live.Set("get", op("存活探针(上游不可达时也应为 200)", nil, nil, nil))
	paths.Set("/livez", live)

	ready := parser.NewObject()
	ready.Set("get", op("就绪探针: 网关已初始化且注册表可用", nil,
		[]any{responseRef("503", "网关未就绪")}, nil))
	paths.Set("/readyz", ready)

	metrics := parser.NewObject()
	metrics.Set("get", op("Prometheus 文本格式的观测指标", nil,
		[]any{responseRef("200", "指标文本")}, nil))
	paths.Set("/metrics", metrics)

	schema := parser.NewObject()
	schema.Set("get", op("meta 规范: 描述 SourceSpec / EndpointSpec / 请求 / 响应信封的字段", nil, nil, nil))
	paths.Set("/v1/schema", schema)

	stats := parser.NewObject()
	stats.Set("get", op("缓存与注册表统计", nil, nil, nil))
	paths.Set("/v1/stats", stats)

	sources := parser.NewObject()
	sources.Set("get", op("源清单(可按 family/q 过滤, 轻量投影)",
		[]any{
			paramQuery("family", "按分类过滤, 模糊匹配", "string", false),
			paramQuery("q", "关键词, 搜源 id/别名/名称/描述/机构/国别/端点 id", "string", false),
			paramQuery("limit", "最多返回多少条", "integer", false),
		}, nil, nil))
	paths.Set("/v1/sources", sources)

	source := parser.NewObject()
	source.Set("get", op("单源完整 meta", []any{
		paramPath("source_id", "源 id"),
	}, nil, []any{responseRef("404", "未知源")}))
	paths.Set("/v1/sources/{source_id}", source)

	endpoint := parser.NewObject()
	endpoint.Set("get", op("端点 meta(请求/响应结构)", []any{
		paramPath("source_id", "源 id"),
		paramPath("endpoint_id", "端点 id"),
	}, nil, []any{responseRef("404", "未知端点")}))
	paths.Set("/v1/sources/{source_id}/endpoints/{endpoint_id}", endpoint)

	// /v1/query 同时支持 GET 与 POST —— 必须写在**同一个** path 对象里。
	// 之前分两次 paths.Set("/v1/query", ...), 后写的把先写的整个覆盖掉,
	// 于是 Go 侧的 OpenAPI 里只剩 post, GET /v1/query "人间蒸发"。
	query := parser.NewObject()
	query.Set("get", op("执行查询(查询参数形式)", []any{
		paramQuery("source", "源 id", "string", true),
		paramQuery("endpoint", "端点 id", "string", true),
		paramQuery("params", "JSON 串", "string", false),
		paramQuery("fields", "逗号分隔", "string", false),
		paramQuery("limit", "最多返回多少条", "integer", false),
		paramQuery("since", "增量窗口起点(ISO 日期)", "string", false),
		paramQuery("timeout_ms", "总预算, 上限 500ms", "integer", false),
	}, nil, []any{
		responseRef("200", "查询信封"),
		responseRef("400", "params 不是合法 JSON"),
		responseRef("404", "未知数据源"),
	}))
	query.Set("post", op("执行查询", nil,
		[]any{requestBody()}, []any{
			responseRef("200", "查询信封"),
			responseRef("404", "未知数据源"),
			responseRef("422", "请求体不合法"),
		}))
	paths.Set("/v1/query", query)

	return paths
}

func componentsObject() *parser.Object {
	comps := parser.NewObject()

	envelopeSchema := parser.NewObject()
	envelopeSchema.Set("type", "object")
	envelopeSchema.Set("required", []any{"meta", "data", "fields"})

	props := parser.NewObject()
	props.Set("meta", parser.NewObjectWith("type", "object"))
	props.Set("data", parser.NewObjectWith("description", "实际数据(列表/对象)"))
	props.Set("fields", parser.NewObjectWith("type", "array"))
	envelopeSchema.Set("properties", props)

	schemas := parser.NewObject()
	schemas.Set("ResponseEnvelope", envelopeSchema)
	comps.Set("schemas", schemas)
	return comps
}

func op(summary string, params []any, body []any, responses []any) *parser.Object {
	o := parser.NewObject()
	o.Set("summary", summary)
	o.Set("responses", responsesObject(responses))
	if len(params) > 0 {
		o.Set("parameters", params)
	}
	if len(body) > 0 {
		o.Set("requestBody", body[0])
	}
	return o
}

func responsesObject(responses []any) *parser.Object {
	o := parser.NewObject()
	if len(responses) == 0 {
		o.Set("200", parser.NewObjectWith("description", "成功"))
		return o
	}
	for _, r := range responses {
		ro, ok := r.(*parser.Object)
		if !ok {
			continue
		}
		code, _ := ro.Get("code")
		desc, _ := ro.Get("description")
		o.Set(fmt.Sprint(code), parser.NewObjectWith("description", fmt.Sprint(desc)))
	}
	return o
}

func responseRef(code, description string) *parser.Object {
	o := parser.NewObject()
	o.Set("code", code)
	o.Set("description", description)
	return o
}

func paramPath(name, description string) *parser.Object {
	o := parser.NewObject()
	o.Set("name", name)
	o.Set("in", "path")
	o.Set("required", true)
	o.Set("description", description)
	o.Set("schema", parser.NewObjectWith("type", "string"))
	return o
}

func paramQuery(name, description, typ string, required bool) *parser.Object {
	o := parser.NewObject()
	o.Set("name", name)
	o.Set("in", "query")
	o.Set("required", required)
	o.Set("description", description)
	o.Set("schema", parser.NewObjectWith("type", typ))
	return o
}

func requestBody() *parser.Object {
	body := parser.NewObject()
	body.Set("required", true)
	content := parser.NewObject()
	schema := parser.NewObject()
	schema.Set("type", "object")
	props := parser.NewObject()
	props.Set("source", parser.NewObjectWith("type", "string"))
	props.Set("endpoint", parser.NewObjectWith("type", "string"))
	props.Set("params", parser.NewObjectWith("type", "object"))
	props.Set("fields", parser.NewObjectWith("type", "array"))
	props.Set("limit", parser.NewObjectWith("type", "integer"))
	props.Set("since", parser.NewObjectWith("type", "string"))
	props.Set("timeout_ms", parser.NewObjectWith("type", "integer"))
	schema.Set("properties", props)
	media := parser.NewObject()
	media.Set("schema", schema)
	content.Set("application/json", media)
	body.Set("content", content)
	return body
}
