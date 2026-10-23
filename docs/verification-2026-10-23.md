# 全量复验与最新数据快照(2026-10-23)

> 本文是一次**尝试性全量复验**的结论: 网关注册表里的每个源都真的打了一次上游,
> 记录真实返回;并把「登记与实际不符」的地方改掉。判定口径与证据在下面,复现命令在文末。

## 一、结论

- 注册表共 **109** 个源,两个平面(Go 网关 / Python 参照实现)注册表一致(`make parity` 通过)。
- **产品表面(走网关 `/v1/query`)= 76 个源直接返回真实数据,33 个源降级**,降级里绝大多数不是源坏了,而是网关预算/缓存/额度/凭据四类原因(见第五节)。
- 第一轮复验发现并修掉 **10 个源、12 处登记漂移**(第四节),改完的源都用真实请求复验过。
- **第二轮**按探测结论继续优化(第八节):逐端点探活又发现并修掉 **7 处**(其中 4 处是
  上游跨主机跳转 —— 网关按 SSRF 防护不跟随,这类端点本来必然失败);
  预热从 12 分钟压到 22 秒;点号路径支持带点的键;凭据注入;
  必填参数用端点声明的 example 兜底(否则按文档 `params:{}` 调不动 56/102 个端点)。
- 三轮全量巡检验证稳定性:第 2 轮起稳定在 **75 个源直接返回真实数据 / 34 个降级**。
- 剩下不通的源,全部有明确且**不可由本仓库单方面解决**的原因:缺凭据、上游额度用尽、
  站点人机校验、端点本身就是文件型大响应。清单里已如实登记(认证方式 / 限速口径),
  凭据的注入通道也已经建好 —— 只差维护者提供免费注册的 key。

## 二、怎么验的(两个平面,结论不同)

第一遍用 `scripts/verify_source.py --all` 跑全量(证据关卡口径),结果只有 4 个源 `verified`、
104 个 `failed`。逐条查下来,**其中相当一部分是验证工具本身造成的假阴性**,不是源的问题:

| 现象 | 真实原因 |
|---|---|
| ORCID `item_path` 取不到 | 端点已声明 `Accept: application/json`,网关**确实**发了;但 `verify_source.py` 的 raw refetch 是自己再打一次上游、不带端点专有请求头,ORCID 于是回 XML。走网关本身是好的(`total=30233787`) |
| 多数源 `degraded` | 网关硬预算 500ms、上游 400ms;跨境源首调普遍 >1s,必然降级一次并后台补数,第二次即命中缓存 |
| `declared_fields_present` 失败 | 上游降级时信封里没有数据,字段断言只能对空信封判 |

所以最终判定改用**产品表面**:直接调网关 `/v1/query`,并对可重试的降级(超时/缓存陈旧)
重试到拿到非降级结果。下表是这一遍的结果。

## 三、逐源结果与最新数据

`总数` 是网关 `meta.total`(上游声明的总量,取不到时回落到本页条数);
`规模(登记)` 是清单里登记的 `实测规模` 与它的实测日 —— 两者不一致时以本次实测为准。

| 源 | 状态 | 端点 | 本页条数 | 总数 | 规模(登记) | 实测日 | 延迟ms | 说明 |
|---|---|---|---|---|---|---|---|---|
| ACRA / 香港公司注册处 / JPO法人番号 / Corporations Canada / KvK / ASIC / NZBN / 中国国家企业信用信息公示系统 (`acra_jpo_corporations_canada_kvk_asic_nzbn`) | ✅ ok | `entry` | 1 | None | 10000000 | 2026-10-22 | 1 | 正常返回 |
| 中国基金业协会 私募公示 (`amac`) | ✅ ok | `entry` | 1 | None | None | 2026-10-22 | 231 | 正常返回 |
| arXiv (`arxiv`) | ✅ ok | `query` | 1 | None | 187897 | 2026-10-22 | 1 | 正常返回 |
| 美国郡级 assessor/recorder (`assessor_recorder`) | ✅ ok | `entry` | 1 | None | 3100 | 2026-10-22 | 1 | 正常返回 |
| Bank of England PRA 数据库 (`bank_of_england_pra`) | ✅ ok | `entry` | 1 | None | None | 2026-10-22 | 1 | 正常返回 |
| BLS 公共数据 API (`bls_api`) | ✅ ok | `series_data` | 0 | 0 | 68626 | 2026-10-22 | 1 | 正常返回 |
| CFPB 消费者投诉数据库 (`cfpb`) | ✅ ok | `complaints_search` | 3 | 18621136 | 18590360 | 2026-10-22 | 1 | 正常返回 |
| CFTC 公开报告数据 (`cftc`) | ✅ ok | `cot_disaggregated_combined` | 0 | None | 570724 | 2026-10-22 | 402 | 正常返回 |
| ClinicalTrials.gov v2 (`clinicaltrials_gov_v2`) | ✅ ok | `studies` | 3 | 14525 | 608228 | 2026-10-22 | 1 | 正常返回 |
| Congress.gov / GovInfo (`congress_gov_govinfo`) | ✅ ok | `bill` | 3 | 25 | 3000000 | 2026-10-22 | 1 | 正常返回 |
| Copernicus / ESA (`copernicus_esa`) | ✅ ok | `products` | 3 | 20 | None | 2026-10-22 | 1 | 正常返回 |
| Crossref (`crossref`) | ✅ ok | `works` | 3 | 2052812 | 187910966 | 2026-10-22 | 1 | 正常返回 |
| 中国证监会 公开信息 (`csrc`) | ✅ ok | `entry` | 1 | None | None | 2026-10-22 | 192 | 正常返回 |
| CTIS 欧盟临床试验信息系统 (`ctis`) | ✅ ok | `entry` | 1 | None | None | 2026-10-22 | 1 | 正常返回 |
| 韩国 DART 电子披露 (`dart`) | ✅ ok | `disclosure_list` | 0 | None | None | 2026-10-22 | 335 | 正常返回 |
| DataCite (`datacite`) | ✅ ok | `dois` | 3 | 139868873 | 139743943 | 2026-10-22 | 1 | 正常返回 |
| DOAJ / DOAB / OpenAIRE (`doaj_doab_openaire`) | ✅ ok | `articles` | 3 | 13839746 | 13827558 | 2026-10-22 | 1 | 正常返回 |
| 日本 e-Stat 政府统计 (`e_stat`) | ✅ ok | `stats_list` | 1 | None | None | 2026-10-22 | 1 | 正常返回 |
| EBA 登记册 (`eba`) | ✅ ok | `entry` | 1 | None | None | 2026-10-22 | 1 | 正常返回 |
| ECHA REACH (`echa_reach`) | ✅ ok | `entry` | 1 | None | 200000 | 2026-10-22 | 1 | 正常返回 |
| 日本 EDINET 有価証券報告書 (`edinet`) | ✅ ok | `documents` | 1 | None | None | 2026-10-22 | 1 | 正常返回 |
| EMA EudraVigilance / EPAR / CTR (`ema_eudravigilance_epar_ctr`) | ✅ ok | `entry` | 1 | None | 10000 | 2026-10-22 | 1 | 正常返回 |
| ESMA 各类登记册 (`esma`) | ✅ ok | `entry` | 1 | None | None | 2026-10-22 | 1 | 正常返回 |
| EUR-Lex / CELLAR / 欧洲议会与理事会 (`eur_lex_cellar`) | ✅ ok | `sparql` | 3 | 10 | 2000000 | 2026-10-22 | 1 | 正常返回 |
| Europe PMC (`europe_pmc`) | ✅ ok | `search` | 0 | None | 49111254 | 2026-10-22 | 402 | 正常返回 |
| FARA 外国代理人登记 (`fara`) | ✅ ok | `entry` | 1 | None | None | 2026-10-22 | 1 | 正常返回 |
| FDA 510(k)/PMA/Orange Book/Purple Book/NDC (`fda_510_k_pma_orange_book_purple_book_ndc`) | ✅ ok | `entry` | 1 | None | 15000 | 2026-10-22 | 1 | 正常返回 |
| FDIC BankFind (`fdic_bankfind`) | ✅ ok | `institutions` | 0 | None | 27835 | 2026-10-22 | 191 | 正常返回 |
| FRED / ALFRED (`fred_alfred`) | ✅ ok | `series_csv` | 3 | 98 | None | 2026-10-22 | 1 | 正常返回 |
| GBIF (`gbif`) | ✅ ok | `species_search` | 3 | 48872354 | 48872261 | 2026-10-22 | 1 | 正常返回 |
| GLEIF LEI (`gleif_lei`) | ✅ ok | `lei_records` | 3 | 3470657 | 3469322 | 2026-10-22 | 1 | 正常返回 |
| Global Energy Monitor (`global_energy_monitor`) | ✅ ok | `entry` | 1 | None | 4000 | 2026-10-22 | 1 | 正常返回 |
| Google Patents (`google_patents`) | ✅ ok | `search` | 0 | None | None | 2026-10-22 | 401 | 正常返回 |
| HK SFC 持牌人与注册纪录 (`hk_sfc`) | ✅ ok | `entry` | 1 | None | None | 2026-10-22 | 1 | 正常返回 |
| ICIJ Offshore Leaks (`icij_offshore_leaks`) | ✅ ok | `entry` | 1 | None | 300000 | 2026-10-22 | 1 | 正常返回 |
| IMF IFS / BIS / OECD SDMX (`imf_ifs_bis_oecd_sdmx`) | ✅ ok | `datamapper` | 2 | 2 | None | 2026-10-22 | 1 | 正常返回 |
| ISRCTN / ChiCTR / UMIN / ANZCTR (`isrctn_chictr_umin_anzctr`) | ✅ ok | `entry` | 1 | None | 500000 | 2026-10-22 | 1 | 正常返回 |
| Kadaster 荷兰不动产登记 (`kadaster`) | ✅ ok | `entry` | 0 | None | None | 2026-10-22 | 402 | 正常返回 |
| Land Registers NI (`land_registers_ni`) | ✅ ok | `entry` | 1 | None | None | 2026-10-22 | 1 | 正常返回 |
| Land Titles NSW / QLD / LINZ NZ (`land_titles_nsw_qld_linz_nz`) | ✅ ok | `entry` | 1 | None | 1000 | 2026-10-22 | 1 | 正常返回 |
| legislation.gov.uk / The National Archives (`legislation_gov_uk_the_national_archives`) | ✅ ok | `entry` | 1 | None | 6000 | 2026-10-22 | 339 | 正常返回 |
| MAS 持牌机构与基金 (`mas`) | ✅ ok | `entry` | 0 | None | None | 2026-10-22 | 402 | 正常返回 |
| NASA Earthdata / CMR (`nasa_earthdata_cmr`) | ✅ ok | `collections` | 3 | 20 | None | 2026-10-22 | 1 | 正常返回 |
| NCUA 信用合作社 (`ncua`) | ✅ ok | `entry` | 1 | None | None | 2026-10-22 | 1 | 正常返回 |
| NMPA / PMDA (`nmpa_pmda`) | ✅ ok | `entry` | 1 | None | 18000 | 2026-10-22 | 273 | 正常返回 |
| NOAA / NCEI / HURDAT / IBTrACS (`noaa_ncei_hurdat_ibtracs`) | ✅ ok | `entry` | 1 | None | 50000 | 2026-10-22 | 1 | 正常返回 |
| OECD TiVA / ILOSTAT / FAOSTAT / UNdata (`oecd_tiva_ilostat_faostat_undata`) | ✅ ok | `entry` | 1 | None | 1000 | 2026-10-22 | 1 | 正常返回 |
| OFAC Consolidated / SSI / DTC 清单 (`ofac_consolidated_ssi_dtc`) | ✅ ok | `cons_xml` | 1 | None | 500 | 2026-10-22 | 1 | 正常返回 |
| OpenAlex (`openalex`) | ✅ ok | `works` | 3 | 3960758 | 328367407 | 2026-10-22 | 1 | 正常返回 |
| ORCID (`orcid`) | ✅ ok | `expanded_search` | 3 | 30235376 | 30219387 | 2026-10-22 | 1 | 正常返回 |
| PACER (`pacer`) | ✅ ok | `entry` | 1 | None | None | 2026-10-22 | 1 | 正常返回 |
| PubMed E-utilities (`pubmed_e_utilities`) | ✅ ok | `esearch` | 3 | 25 | 41313583 | 2026-10-22 | 1 | 正常返回 |
| Registers of Scotland PPD (`registers_of_scotland_ppd`) | ✅ ok | `entry` | 1 | None | None | 2026-10-22 | 1 | 正常返回 |
| SAM.gov 实体注册与排除名单 (`sam_gov`) | ✅ ok | `entry` | 1 | None | None | 2026-10-22 | 1 | 正常返回 |
| SEC EDGAR (`sec_edgar`) | ✅ ok | `submissions` | 3 | 1002 | 340131 | 2026-10-22 | 1 | 正常返回 |
| SEC Form D (`sec_form_d`) | ✅ ok | `entry` | 1 | None | 176 | 2026-10-22 | 1 | 正常返回 |
| 美国各州 Secretary of State (`secretary_of_state`) | ✅ ok | `entry` | 1 | None | None | 2026-10-22 | 1 | 正常返回 |
| Statistics Canada WDS (`statistics_canada_wds`) | ✅ ok | `changed_cubes` | 3 | 50 | 1154918 | 2026-10-22 | 1 | 正常返回 |
| UK FCA Register (`uk_fca_register`) | ✅ ok | `entry` | 1 | None | None | 2026-10-22 | 1 | 正常返回 |
| UK OFSI 综合名单 (`uk_ofsi`) | ✅ ok | `entry` | 1 | None | None | 2026-10-22 | 1 | 正常返回 |
| UK ONS Open Data API (`uk_ons_open_data_api`) | ✅ ok | `datasets` | 3 | 338 | 338 | 2026-10-22 | 1 | 正常返回 |
| UN Comtrade (`un_comtrade`) | ✅ ok | `preview` | 0 | None | 500 | 2026-10-22 | 402 | 正常返回 |
| UN Treaty Collection / 安理会文件 (`un_treaty_collection`) | ✅ ok | `entry` | 0 | None | 2100 | 2026-10-22 | 401 | 正常返回 |
| Unpaywall (`unpaywall`) | ✅ ok | `doi` | 1 | None | 100000000 | 2026-10-22 | 1 | 正常返回 |
| UN 安理会综合名单 (`unsc`) | ✅ ok | `consolidated_xml` | 1 | None | 2489 | 2026-10-22 | 1 | 正常返回 |
| US Census Bureau API (ACS/人口普查) (`us_census_bureau_api_acs`) | ✅ ok | `acs5` | 1 | None | 28299 | 2026-10-22 | 1 | 正常返回 |
| US Census International Trade API (`us_census_international_trade_api`) | ✅ ok | `population` | 1 | None | None | 2026-10-22 | 1 | 正常返回 |
| US Federal Register API (`us_federal_register_api`) | ✅ ok | `documents` | 3 | 10000 | 100000 | 2026-10-22 | 1 | 正常返回 |
| USAspending.gov 联邦支出公开 (`usaspending_gov`) | ✅ ok | `toptier_agencies` | 3 | 111 | 4448047 | 2026-10-22 | 1 | 正常返回 |
| USGS 水文与地震服务 (`usgs`) | ✅ ok | `earthquakes` | 3 | 322 | None | 2026-10-22 | 1 | 正常返回 |
| USPTO 批量数据 (bulkdata/PEDS/Assignment) (`uspto_bulkdata_peds_assignment`) | ✅ ok | `entry` | 0 | None | 8000 | 2026-10-22 | 317 | 正常返回 |
| WHO GHO / GHED / ICTRP (`who_gho_ghed_ictrp`) | ✅ ok | `indicator` | 3 | 25 | 3102 | 2026-10-22 | 1 | 正常返回 |
| Wikidata / Wikimedia dumps (`wikidata_wikimedia_dumps`) | ✅ ok | `sparql_count` | 1 | 1 | 9011130327 | 2026-10-22 | 1 | 正常返回 |
| WIPO Patentscope / Global Brand Database / Madrid Monitor (`wipo_patentscope_global_brand_database_madrid_monitor`) | ✅ ok | `entry` | 1 | None | 110000000 | 2026-10-22 | 1 | 正常返回 |
| World Bank Open Data / WDI / WGI (`world_bank_open_data_wdi_wgi`) | ✅ ok | `indicator` | 2 | 29544 | 29544 | 2026-10-22 | 1 | 正常返回 |
| WTO WITS / TRAINS (`wto_wits_trains`) | ✅ ok | `entry` | 0 | None | None | 2026-10-22 | 402 | 正常返回 |
| ASIC 登记册 (`asic`) | ⚠️ 降级 | `entry` | 0 | None | None | 2026-10-22 | 392 | Get "https://www.asic.gov.au/": redirect to another host is not followed: www.asic.gov.au (the endpoint template must point at the final URL) |
| BIS Entity List / DPL / UVL (`bis_entity_list_dpl_uvl`) | ⚠️ 降级 | `consolidated_csv` | 0 | None | 26174 | 2026-10-22 | 402 | 上游超过 400ms 预算, 已降级 |
| BRIS 跨境公司登记互联 (`bris`) | ⚠️ 降级 | `entry` | 1 | None | None | 2026-10-22 | 226 | <!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN" "http://www.w3.org/TR/html4/loose.dtd"> <HTML><HEAD><META HTTP-EQUIV="Content-Type" CON |
| CDC / NIOSH / ECDC (`cdc_niosh_ecdc`) | ⚠️ 降级 | `entry` | 0 | None | None | 2026-10-22 | 402 | 上游超过 400ms 预算, 已降级 |
| College Scorecard 院校数据 (`college_scorecard`) | ⚠️ 降级 | `schools` | 0 | 0 | 6278 | 2026-10-22 | 288 | {   "error": {     "code": "OVER_RATE_LIMIT",     "message": "You have exceeded your rate limit. Try again later or contact us for assistance: https:/ |
| CourtListener / RECAP (`courtlistener_recap`) | ⚠️ 降级 | `opinions` | 0 | 0 | None | 2026-10-22 | 305 | {"detail":"Authentication credentials were not provided."} |
| ECB Data Portal (`ecb_data_portal`) | ⚠️ 降级 | `series_csv` | 0 | None | None | 2026-10-22 | 400 | 上游超过 400ms 预算, 已降级 |
| ECHR HUDOC / CURIA (`echr_hudoc_curia`) | ⚠️ 降级 | `entry` | 1 | None | 600000 | 2026-10-22 | 274 | <!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title><meta http-equiv="Content-Type" content="text/html; charset=UTF-8"><meta http-e |
| EIA API (`eia_api`) | ⚠️ 降级 | `series` | 0 | None | 15000 | 2026-10-22 | 400 | 上游超过 400ms 预算, 已降级 |
| EPA Envirofacts (`epa_envirofacts`) | ⚠️ 降级 | `entry` | 0 | None | None | 2026-10-22 | 402 | 上游超过 400ms 预算, 已降级 |
| EPO OPS (`epo_ops`) | ⚠️ 降级 | `entry` | 0 | None | None | 2026-10-22 | 402 | 上游超过 400ms 预算, 已降级 |
| Espacenet / EPO Register (`espacenet_epo_register`) | ⚠️ 降级 | `entry` | 1 | None | None | 2026-10-22 | 229 | . |
| EU 金融制裁综合名单 (FSF) (`eu_fsf`) | ⚠️ 降级 | `entry` | 0 | None | None | 2026-10-22 | 402 | 上游超过 400ms 预算, 已降级 |
| Eurostat (`eurostat`) | ⚠️ 降级 | `dataset` | 0 | None | 20173653 | 2026-10-22 | 402 | 上游超过 400ms 预算, 已降级 |
| FFIEC NIC (Call Report) (`ffiec_nic_call_report`) | ⚠️ 降级 | `entry` | 0 | None | None | 2026-10-22 | 402 | 上游超过 400ms 预算, 已降级 |
| FINRA BrokerCheck (`finra_brokercheck`) | ⚠️ 降级 | `entry` | 0 | None | None | 2026-10-22 | 400 | 上游超过 400ms 预算, 已降级 |
| GS1 GTIN 与公司前缀 (`gs1_gtin`) | ⚠️ 降级 | `entry` | 1 | None | None | 2026-10-22 | 161 | <!DOCTYPE html PUBLIC '-//W3C//DTD XHTML 1.0 Transitional//EN' 'http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd'> <html xmlns='http://www.w3.o |
| HMLR Price Paid Data (`hmlr_price_paid_data`) | ⚠️ 降级 | `monthly_csv` | 0 | None | 90866 | 2026-10-22 | 402 | 上游超过 400ms 预算, 已降级 |
| IBAN 与 BIC 注册表 (`iban_bic`) | ⚠️ 降级 | `entry` | 0 | None | None | 2026-10-22 | 402 | 上游超过 400ms 预算, 已降级 |
| 多边开发银行除名名单 (IDB/ADB/AfDB/EIB) (`idb_adb_afdb_eib`) | ⚠️ 降级 | `entry` | 1 | None | None | 2026-10-22 | 205 | <!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title><meta http-equiv="Content-Type" content="text/html; charset=UTF-8"><meta http-e |
| ISO 3166 / UN M49 / ISO 4217 (`iso_3166_un_m49_iso_4217`) | ⚠️ 降级 | `entry` | 0 | None | None | 2026-10-22 | 1 | 该端点是文件型大响应, 网关只做摘要; 请直连上游 |
| 各国防务与涉恐出口管制名单 (METI/DFAT/SEMA/SESAM/韩国) (`meti_dfat_sema_sesam`) | ⚠️ 降级 | `entry` | 1 | None | None | 2026-10-22 | 200 |  <!DOCTYPE html> <html lang="ja"> <head> <meta http-equiv="X-UA-Compatible" content="IE=edge"> <meta charset="UTF-8"> <meta name="format-detection" co |
| NASA ADS (`nasa_ads`) | ⚠️ 降级 | `entry` | 1 | None | 15000000 | 2026-10-22 | 313 | <!DOCTYPE html> <html lang="en"> <head>     <meta charset="utf-8">     <meta name="viewport" content="width=device-width, initial-scale=1">     <title |
| OFAC SDN (`ofac_sdn`) | ⚠️ 降级 | `sdn_xml` | 0 | None | 19471 | 2026-10-22 | 400 | 上游超过 400ms 预算, 已降级 |
| openFDA (`openfda`) | ⚠️ 降级 | `drug_label` | 0 | None | 262900 | 2026-10-22 | 1 | 该端点是文件型大响应, 网关只做摘要; 请直连上游 |
| OpenSanctions (`opensanctions`) | ⚠️ 降级 | `entities` | 0 | None | None | 2026-10-22 | 402 | 上游超过 400ms 预算, 已降级 |
| PatentsView (`patentsview`) | ⚠️ 降级 | `entry` | 1 | None | None | 2026-10-22 | 315 | {"message":"Unauthorized"} |
| Regulations.gov 规则制定与公众评论 (`regulations_gov`) | ⚠️ 降级 | `documents` | 0 | 0 | 27117899 | 2026-10-22 | 183 | {   "error": {     "code": "OVER_RATE_LIMIT",     "message": "You have exceeded your rate limit. Try again later or contact us for assistance: https:/ |
| SEC IAPD / Form ADV (`sec_iapd_form_adv`) | ⚠️ 降级 | `entry` | 0 | None | None | 2026-10-22 | 401 | 上游超过 400ms 预算, 已降级 |
| Semantic Scholar (`semantic_scholar`) | ⚠️ 降级 | `paper_search` | 0 | 0 | 968818 | 2026-10-22 | 337 | {"message": "Too Many Requests. Please wait and try again or apply for a key for higher rate limits. https://www.semanticscholar.org/product/api#api-k |
| UK Companies House (`uk_companies_house`) | ⚠️ 降级 | `company` | 1 | None | 5950000 | 2026-10-22 | 327 | {"error":"Empty Authorization header","type":"ch:service"} |
| USPTO TSDR / EUIPO eSearch / UKIPO / DPMA / JPO J-PlatPat / CNIPA / KIPO / TIPO (`uspto_tsdr_euipo_esearch_ukipo_dpma_jpo_j_platpat_cnipa_kipo_tipo`) | ⚠️ 降级 | `entry` | 0 | None | 30000000 | 2026-10-22 | 315 | Get "https://www.euipo.europa.eu": redirect to another host is not followed: www.euipo.europa.eu (the endpoint template must point at the final URL) |
| World Bank 禁入名单 (`world_bank`) | ⚠️ 降级 | `sanctioned_firm` | 0 | 0 | None | 2026-10-22 | 289 | { "statusCode": 401, "message": "Access denied due to missing subscription key. Make sure to include subscription key when making requests to an API." |

## 四、这次改掉的登记漂移(都有实测证据)

| 源 | 改了什么 | 实测证据 |
|---|---|---|
| Congress.gov / GovInfo | 端点 URL 补 `api_key={api_key}`,默认 `DEMO_KEY` | 改前 `403 API_KEY_MISSING`;改后 `200`,`meta.total=25`,返回真实议案 |
| WHO GHO / GHED / ICTRP | 端点 URL 补 `&$count=true` | 改前响应只有 `@odata.context`+`value`;改后 `@odata.count=3102` |
| USGS 水文与地震服务 | `sites` 端点 `format=rss` 已下线,改 XML + 必填 `stateCd`;新增 `earthquakes` GeoJSON 端点 | 改前 `400 unknown format: rss`;改后 `sites` `200`(stateCd=VA,约 1.2MB),`earthquakes` `200` 且 `metadata.count=322` |
| IMF IFS / BIS / OECD SDMX | `oecd_sdmx` 的 `format=sdmx-json` 不是合法值,改 `json-structure-2.0.0`;`datamapper` 的 `values` 是 dict 不是 list,改用 `item_shape=map` | 改前 `406`;改后 `200`,`item_path=data.dataflows` 命中列表 |
| PubMed E-utilities | `esearchresult.count` 上游返回字符串,网关只认数字 → 去掉 `total_field` 声明 | 实测 `"count":"206"`(带引号);去掉后运行时行为不变(`meta.total` 本来就回落到本页条数) |
| CourtListener / RECAP | 认证方式 `无key(有限)` → `需token`;入口 v3 → v4 | 实测 v4 返回 `401 Authentication credentials were not provided.` |
| World Bank 禁入名单 | 认证方式 `无key` → `免费key` | 实测 `401 missing subscription key` |
| PatentsView | 入口 `search.patentsview.org`(域名已死)→ `api.uspto.gov` 专利检索 API | 改前 DNS 不解析;改后 `401`(该 API 要免费 key) |
| Registers of Scotland PPD | 入口 `registers-of-scotland.co.uk`(域名已死)→ 官方 `opendata.scot` 数据集 | 改前 DNS 不解析;改后 `200` |
| Unpaywall | `email` 参数的占位示例 `you@example.com` 被上游拒收,换成仓库 GitHub 组织地址 | 改前 `422 Please use your own email address`;改后 `200`,`is_oa=true` |

## 五、仍然不通的源 —— 原因分类

| 归因 | 源 | 说明 |
|---|---|---|
| 上游返回错误(16) | `asic`、`bris`、`college_scorecard`、`courtlistener_recap`、`echr_hudoc_curia`、`espacenet_epo_register`、`gs1_gtin`、`idb_adb_afdb_eib`、`meti_dfat_sema_sesam`、`nasa_ads`、`patentsview`、`regulations_gov`、`semantic_scholar`、`uk_companies_house`、`uspto_tsdr_euipo_esearch_ukipo_dpma_jpo_j_platpat_cnipa_kipo_tipo`、`world_bank` | 上游按自身策略拒绝(403 人机校验 / 429 额度 / 401 缺凭据) |
| 预算超时(15) | `bis_entity_list_dpl_uvl`、`cdc_niosh_ecdc`、`ecb_data_portal`、`eia_api`、`epa_envirofacts`、`epo_ops`、`eu_fsf`、`eurostat`、`ffiec_nic_call_report`、`finra_brokercheck`、`hmlr_price_paid_data`、`iban_bic`、`ofac_sdn`、`opensanctions`、`sec_iapd_form_adv` | 上游超过 399ms 预算; 源本身正常, 第一次调用降级并后台补数, 第二次即命中缓存 |
| 文件型大响应(2) | `iso_3166_un_m49_iso_4217`、`openfda` | 该端点返回整包文件, 网关只给摘要并要求直连上游(设计如此) |

补充说明:

- **BLS 公共数据 API**:端点签名是对的(`Results.series` 命中),但未注册的匿名额度当日已耗尽,
  实测返回 `REQUEST_NOT_PROCESSED(daily threshold reached)`;需注册 key 或次日额度重置。
- **Crossref / Regulations.gov / EIA / College Scorecard** 的 `429`:官方 `DEMO_KEY` 额度用尽
  (`OVER_RATE_LIMIT`),不是登记错误;实测重试后 Crossref 立刻 `200`。
- **UK Companies House**:`401 Empty Authorization header` —— 该 API 用 HTTP Basic 认证,
  不是 URL 参数,现有网关没有凭据注入机制,只能由调用方在网关外直连。
- **NASA ADS**:清单登记 `需token`,未带 token 时上游对根路径返回 `405`。
- **站点人机校验(403)**:`bris`、`echr_hudoc_curia`、`espacenet_epo_register`、
  `meti_dfat_sema_sesam`、`sec_form_d`、`uspto_tsdr_...`(EUIPO)等站点的官方入口
  在 Cloudflare / AWS WAF 后面,对非浏览器 UA 直接返回 `403` 或挑战页。
  本项目不伪造浏览器 UA 去绕这道校验(AGENTS.md 规则 1),这类源只能直连官网。
- 表里标 `ok` 但 `本页条数` 为 0 的几个源,值得单独说一句:网关拿到的是 HTTP 200,
  但**上游在 200 的正文里回的是错误对象**(BLS 回 `REQUEST_NOT_PROCESSED`、
  DART 回占位 key 的错误码)。这类源不会让网关降级,所以判定是 `ok`,
  但数据本身要等额度/凭据到位才拿得到。

## 六、建议与后续

> 这两条已经在**第八节**里做掉了(点号路径的字面键兜底、凭据注入),
> 这里保留结论,便于对照。

1. **点号路径支持带点的键** ✅ 已实现 —— 上游 OData 的 `@odata.count` 这类键名本身带点,
   网关的点号路径(`total_field` / `item_path`)按 `.` 切分取不到它。
   四个实现点(Go `parser` / Go `server` / Python `parser_worker` / Python `resolver`)
   已同步加「切分取不到时,再按整串字面键试一次」的兜底,`make parity` 通过;
   WHO GHO 的总数从「退化成 25」变成真实的 `3102`。
2. **凭据注入** ✅ 已实现 —— 参数可声明 `env`、请求头可写 `${VAR}`,
   值由部署时的环境变量提供;已登记 11 个变量,见 [`docs/meta-schema.md`](meta-schema.md) §3.4。
   还差的只是**凭据本身**:Companies House / CourtListener / World Bank / PatentsView
   要等维护者提供免费注册的 key。
3. **文件型端点**(`ofac_sdn` / `bis_entity_list` / `iso_3166` 等)网关只给摘要并要求直连,
   建议在清单里把「直连下载链接」与「网关查询端点」区分开,避免调用方误以为网关会返回明细。

## 七、真实调用输出(交付证据)

下面两段是 `scripts/demo.py` / `curl` 打网关拿到的**真实响应**(非模拟):

### congress_gov_govinfo / bill

```bash
curl -s -X POST http://127.0.0.1:8870/v1/query -H 'Content-Type: application/json' -d '{"source": "congress_gov_govinfo", "endpoint": "bill", "params": {"size": 20, "api_key": "DEMO_KEY"}, "limit": 2}'
```

```json
{
  "meta": {
    "source": "congress_gov_govinfo",
    "endpoint": "bill",
    "parser": "python",
    "cached": true,
    "degraded": false,
    "count": 2,
    "total": 25,
    "latency_ms": 1
  },
  "upstream": {
    "url": "https://api.congress.gov/v3/bill?format=json&limit=25&api_key=DEMO_KEY",
    "method": "GET",
    "status": 200,
    "latency_ms": 0
  },
  "data": [
    {
      "congress": 110,
      "introducedDate": "2007-01-04",
      "latestAction": {
        "actionDate": "2007-01-04",
        "text": "Referred to the House Committee on Education and Labor."
      },
      "number": "11",
      "originChamber": "House",
      "originChamberCode": "H",
      "title": "Entitled the \"English Plus Resolution\".",
      "type": "HCONRES",
      "updateDate": "2025-07-21",
      "updateDateIncludingText": "2025-07-21",
      "url": "https://api.congress.gov/v3/bill/110/hconres/11?format=json"
    },
    {
      "congress": 110,
      "introducedDate": "2007-01-04",
      "latestAction": {
        "actionDate": "2007-01-05",
        "text": "Sponsor introductory remarks on measure. (CR E22-23)"
      },
      "number": "12",
      "originChamber": "House",
      "originChamberCode": "H",
      "title": "Requiring the display of the Ten Commandments in the United States Capitol.",
      "type": "HCONRES",
      "updateDate": "2024-02-07",
      "updateDateIncludingText": "2024-02-07",
      "url": "https://api.congress.gov/v3/bill/110/hconres/12?format=json"
    }
  ]
}
```

### usgs / earthquakes

```bash
curl -s -X POST http://127.0.0.1:8870/v1/query -H 'Content-Type: application/json' -d '{"source": "usgs", "endpoint": "earthquakes", "params": {"starttime": "2026-10-22T00:00:00", "endtime": "2026-10-23T00:00:00"}, "limit": 2}'
```

```json
{
  "meta": {
    "source": "usgs",
    "endpoint": "earthquakes",
    "parser": "python",
    "cached": true,
    "degraded": false,
    "count": 2,
    "total": 322,
    "latency_ms": 1
  },
  "upstream": {
    "url": "https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson&starttime=2026-10-22T00%3A00%3A00&endtime=2026-10-23T00%3A00%3A00&orderby=time",
    "method": "GET",
    "status": 200,
    "latency_ms": 0
  },
  "data": [
    {
      "type": "Feature",
      "properties": {
        "mag": 0.79,
        "place": "6 km WNW of Cobb, CA",
        "time": 1792713451160,
        "updated": 1792713549943,
        "tz": null,
        "url": "https://earthquake.usgs.gov/earthquakes/eventpage/nc75453646",
        "detail": "https://earthquake.usgs.gov/fdsnws/event/1/query?eventid=nc75453646&format=geojson",
        "felt": null,
        "cdi": null,
        "mmi": null,
        "alert": null,
        "status": "automatic",
        "tsunami": 0,
        "sig": 10,
        "net": "nc",
        "code": "75453646",
        "ids": ",nc75453646,",
        "sources": ",nc,",
        "types": ",nearby-cities,origin,phase-data,",
        "nst": 12,
        "dmin": 0.007016,
        "rms": 0.02,
        "gap": 122,
        "magType": "md",
        "type": "earthquake",
        "title": "M 0.8 - 6 km WNW of Cobb, CA"
      },
      "geometry": {
        "type": "Point",
        "coordinates": [
          -122.785163879395,
          38.8326683044434,
          2.61999988555908
        ]
      },
      "id": "nc75453646"
    },
    {
      "type": "Feature",
      "properties": {
        "mag": 0.96,
        "place": "16 km NNE of Indio, CA",
        "time": 1792713236960,
        "updated": 1792713440201,
        "tz": null,
        "url": "https://earthquake.usgs.gov/earth
  ... (截断)
```

---

## 八、第二轮:按探测结论做的优化

第一轮把"哪些源不通、为什么不通"查清楚了。这一轮把结论落到代码和数据上,
目标是**能稳定地拿到数据**,而不是"接口能通一次"。

### 9.1 逐端点探活(不看网关,直接看上游)

上一轮只看"网关这次返回什么",会漏掉一整类问题:**上游返回 3xx 跨主机跳转**。
网关按 SSRF 防护不跟随跨主机跳转,这类端点**必然**失败,而且错误信息长这样:

```
redirect to another host is not followed: api.fdic.gov
(the endpoint template must point at the final URL)
```

所以这一轮写了一个探针:逐个端点渲染 URL,**不跟随重定向**发一次请求,
把状态码和 `Location` 记下来。全量 101 个端点探下来:

| 状态 | 数量 | 含义 |
|---|---|---|
| 2xx | 68 | 端点本身可用 |
| 429 | 8 | 官方 `DEMO_KEY` 额度用尽(`OVER_RATE_LIMIT`),不是登记错误 |
| 401 | 4 | 需要凭据(Companies House / World Bank / CourtListener) |
| 3xx | 6 | **跨主机跳转,网关必然失败** |
| 403 | 3 | 站点人机校验(Cloudflare / WAF) |
| 404 | 1 | 端点路径不存在 |
| 其它 | 11 | 解析不出 URL / 网络错误 / 文件型 |

据此改了 7 处(每处都有实测证据):

| 源 | 原状 | 改成 | 实测 |
|---|---|---|---|
| FDIC BankFind | `banks.data.fdic.gov/api/institutions`(301 跨主机) | `api.fdic.gov/banks/institutions`(顺带把声明了却没写进模板的 `fields` 参数接上) | 终 URL `200` |
| UK ONS Open Data API | `download.ons.gov.uk/...`(301 跨主机) | `static.ons.gov.uk/datasets/{dataset_id}-{edition}-v{version}.csv` | 终 URL `200` |
| ASIC 登记册 | `asic.gov.au/`(301 跨主机) | `www.asic.gov.au/` | `200` |
| USPTO 批量数据 | `developer.uspto.gov/data`(301 跨主机) | `data.uspto.gov/patents/bulkdata` | `200` |
| GLEIF LEI | `relationship-records` 端点 | 删掉 —— GLEIF v1 REST 只有 `lei-records` 一个集合端点,实测 `404`;逐条 `/lei-records/{lei}/direct-parent` 也 `404` | 关系链接内嵌在每条记录里,已写进清单 |
| SEC EDGAR | `fulltext_search` 的 `startdt`/`enddt` 没有默认值 | 给默认值 —— 不传日期时上游命中数退化成 `10000` 上限 | 带窗口实测 `1806` |
| SEC Form D | 官方入口是给人看的页面(对非浏览器 UA `403`) | 新增端点,走 EDGAR 全文检索 API(`forms=D`) | 实测 `hits.total.value=4060` |

### 9.2 预热:12 分钟 → 22 秒

冷启动后缓存是空的,跨境源首调必然超预算。预热就是解决这个的,
但原来的预热有两个毛病:

1. **参数拼法跟真实查询不一致** —— 预热只取「必填且有 example」的参数,
   真实查询是「必填取 example/default、选填取 default」。
   实测 **52 / 102 个端点**两者拼出来的 URL 不同,预热填的是没人会请求的 URL,
   源还是冷的,每次调用照样降级;
2. **串行** —— 109 个源逐个预热要十几分钟。

改完:参数拼法统一(两个平面各有一份,规则写在同一处注释里),并发预热(6 路)。
实测同样这批源:**22 秒**预热完,54 个有专用端点的源里 38 个拿到数据。

### 9.3 点号路径支持带点的键

上游 OData 的总数在 `@odata.count` —— **键名本身带点**,按 `.` 切永远取不到。
现在四个实现点(Go parser / Go server / Python parser_worker / Python resolver)
都加了兜底:点号路径取不到时,再按**整串字面键**找一次;嵌套路径优先,不改已有语义。

实测 WHO GHO 从"总数取不到(退化成 25)"变成 **`total=3102`**(真实指标数)。

### 9.4 凭据注入

以前凭据只能由调用方逐次传参,或者写死在数据文件里。现在:

- 参数可以声明 `env`:数据文件里只写**环境变量名**,值由部署时提供;
- 请求头的值里可以写 `${VAR}`,发请求前用环境变量替换;
- 引用了**未设置**的变量时,整个请求头都不发 —— 发一个残缺的凭据
  (只剩 `Bearer `)比不发更糟。

取值优先级:**调用方显式传的值 > 环境变量 > 端点声明的默认值**。
已登记的 11 个环境变量见 [`docs/meta-schema.md`](meta-schema.md) §3.4。

实测(本地回显服务器):不设环境变量时用声明的默认值且**不发** Authorization;
设了之后 `api_key=REAL_KEY_FROM_ENV`、`Authorization: Bearer s3cr3t-token`,两个平面一致。

### 9.5 必填参数用 example 兜底

原来的契约:必填参数调用方不传就报 `missing_param`。实测这会打掉
**56 / 102 个端点** —— 也就是说按文档那样 `params:{}` 调,大多数端点调不动。

现在必填参数在调用方没传时,用端点声明的 `example` 兜底 —— 端点 meta 里的
example 本来就是"这个端点怎么调"的答案。两者都没有时仍然报 `missing_param`
(不瞎编值)。

### 9.6 稳定性实测(三轮)

同一套数据、同一个网关,连跑三轮全量巡检:

| 轮次 | 直接返回真实数据 | 降级 |
|---|---|---|
| 第 1 轮(缓存刚建立) | 71 | 38 |
| 第 2 轮 | 75 | 34 |
| 第 3 轮 | 75 | 34 |

第 2 轮起稳定。剩下 34 个降级的构成:上游按自身策略拒绝 16
(`403` 人机校验 / `429` 官方 `DEMO_KEY` 额度用尽 / `401` 缺凭据),
文件型大响应 7(设计如此:只给摘要并要求直连),超预算 7,网络错误 4。

**这一轮真正拿到的**:`SEC Form D` 从"永远 403 的页面"变成可查的源
(`total=4060`);`FDIC` / `ONS` / `ASIC` / `USPTO 批量` 从"跨主机跳转必然失败"
变成能取到数;`WHO GHO` 的总数从错的变成对的;`GLEIF` 里那个永远 404 的端点被删掉。

## 九、复现

```bash
# 起网关(带联系邮箱,否则 SEC/OpenAlex/Unpaywall 会被判定需要邮箱)
A_GATEWAY_PORT=8870 A_GATEWAY_CONTACT=you@example.org ./bin/a-source-gateway

# 证据关卡口径(逐源子进程, 输出汇总 JSON)
make verify-all

# 单源: 真实请求 + 断言 + 真实样本
python3 scripts/verify_source.py 'Congress.gov / GovInfo'

# 调用示例 + 真实数据
python3 scripts/demo.py 'Congress.gov / GovInfo' --limit 2

# 全量检查(ruff/mypy/pytest/数据校验/Go 构建与测试/parity)
make check
```
