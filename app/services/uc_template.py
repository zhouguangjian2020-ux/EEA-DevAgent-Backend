"""Static, versioned contract for the Agent engineer; no business DB access."""
from app.schemas.agent import UcCandidateContent, UcTemplateField, UcTemplateResponse

UC_TEMPLATE_ID = "uc-default"
UC_TEMPLATE_VERSION = "1"
UC_TEMPLATE_PATH = "/v1/agent/uc-template?scene=FROM_PRD_CREATE_UC"


def uc_template_response() -> UcTemplateResponse:
    return UcTemplateResponse(
        scene="FROM_PRD_CREATE_UC",
        template_id=UC_TEMPLATE_ID,
        template_version=UC_TEMPLATE_VERSION,
        fields=[
            UcTemplateField(name="name", label="用例名称", value_type="string", required=True),
            UcTemplateField(name="description", label="用例描述", value_type="string", required=True),
            UcTemplateField(name="number", label="用例编号", value_type="string", generated_by="backend", description="正式保存时由后端生成；Agent候选必须省略，不返回空编号或假编号。"),
            UcTemplateField(name="actors", label="参与者", value_type="string", required=True),
            UcTemplateField(name="preconditions", label="前置条件", value_type="string", required=True),
            UcTemplateField(name="scenario", label="场景", value_type="object", required=True, children=[
                UcTemplateField(name="main_flow", label="主事件流", value_type="string", required=True),
                UcTemplateField(name="branches", label="分支事件流", value_type="array", required=True, description="string[]，按PRD确定数量，可为空，不固定4条；最多100条。"),
                UcTemplateField(name="exceptions", label="异常事件流", value_type="array", required=True, description="string[]，按PRD确定数量，可为空，不固定5条；最多100条。"),
            ]),
            UcTemplateField(name="postconditions", label="后置条件", value_type="string", required=True),
            UcTemplateField(name="extensions", label="扩展用例", value_type="string", required=True),
            UcTemplateField(name="hmi", label="HMI交互逻辑", value_type="string", required=True),
            UcTemplateField(name="regulations", label="法规需求", value_type="string", required=True),
            UcTemplateField(name="performance", label="性能需求", value_type="string", required=True),
            UcTemplateField(name="nonfunctional", label="非功能需求", value_type="string", required=True),
        ],
        candidate_schema=UcCandidateContent.model_json_schema(),
        instructions=[
            "业务UC是12个顶层字段；场景嵌套主流程/分支/异常，不是14个扁平字段。",
            "Agent返回structured.use_cases数组；每个候选包含除number之外的11个内容字段。",
            "candidate_id、ordinal由业务后端临时分配，不是正式对象ID或UC编号。",
            "未明确的事实用<待确认>或<待标定>标记，不编造事实；无适用分支或异常用[]。",
            "只生成供审阅的候选，不保存、不确认、不调用业务写入接口。",
            "candidate_schema描述TS接口契约，不是Agent技能文件output.schema或新的invoke顶层字段。",
        ],
    )
