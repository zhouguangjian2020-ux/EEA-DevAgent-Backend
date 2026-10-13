BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
DO $preflight$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=current_user AND rolsuper) THEN
  RAISE EXCEPTION 'Install requires an explicitly authorized database administrator';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='ee_m0_owner' AND NOT rolcanlogin AND NOT rolsuper AND NOT rolbypassrls) OR
    NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='ee_project_api_rw' AND rolcanlogin AND NOT rolsuper AND NOT rolbypassrls AND NOT rolcreaterole AND NOT rolinherit) THEN
  RAISE EXCEPTION 'Unexpected role baseline';
 END IF;
 IF to_regprocedure('ee.initialize_default_subsystems_v1(uuid,uuid,text)') IS NOT NULL THEN
  RAISE EXCEPTION 'Initializer already exists; verify it instead of overwriting';
 END IF;
END $preflight$;

CREATE FUNCTION ee.initialize_default_subsystems_v1(p_project uuid, p_actor uuid, p_request_id text)
RETURNS TABLE(design_version_id uuid, item_count integer, template_sha256 text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, ee, pg_temp
AS $body$
DECLARE
 template constant jsonb := '[{"code":"SS-001","name":"外灯控制系统","description":"提供位置灯、昼行灯、近光灯、远光灯、后雾灯、转向灯、倒车灯、制动灯、角灯、后视镜警示灯、后视镜水坑灯、车门照地灯、logo灯、充电口照明灯状态，大灯高度调节状态；\n控制位置灯、昼行灯、近光灯、远光灯、后雾灯、转向灯、倒车灯、制动灯、角灯、后视镜警示灯、后视镜水坑灯、车门照地灯、logo灯、充电口照明灯、大灯高度调节，\n光线传感器；","responsibility_in":null,"responsibility_out":null,"domain_group":"车身\n（1XX）","is_allocatable":true,"sort_order":0},{"code":"SS-002","name":"内灯控制系统","description":"提供顶灯、阅读灯、背光灯、后备箱灯状态；\n控制顶灯、阅读灯、背光灯、后备箱灯、化妆镜灯、手套箱灯、氛围灯提供；","responsibility_in":null,"responsibility_out":null,"domain_group":"车身\n（1XX）","is_allocatable":true,"sort_order":1},{"code":"SS-003","name":"雨刮控制系统","description":"提供前、后雨刮运动状态，前、后洗涤状态；\n控制前雨刮/后雨刮/前洗涤/后洗涤；\n雨量传感器","responsibility_in":null,"responsibility_out":null,"domain_group":"车身\n（1XX）","is_allocatable":true,"sort_order":2},{"code":"SS-004","name":"防盗认证系统","description":"提供蓝牙钥匙位置、蓝牙钥匙认证、蓝牙钥匙管理、NFC钥匙管理、UWB钥匙管理、车身防盗、防盗认证、遥控钥匙认证、遥控钥匙信息提醒状态信息；","responsibility_in":null,"responsibility_out":null,"domain_group":"车身\n（1XX）","is_allocatable":true,"sort_order":3},{"code":"SS-005","name":"锁系统","description":"提供四门两盖、充电口盖、儿童锁、四门锁、前舱锁、后尾门锁状态；\n控制儿童锁、四门锁、前舱锁、后尾门锁的解闭锁；","responsibility_in":null,"responsibility_out":null,"domain_group":"车身\n（1XX）","is_allocatable":true,"sort_order":4},{"code":"SS-006","name":"车窗控制系统","description":"提供四门车窗、天窗、遮阳帘、变色车顶的状态；\n控制车窗、天窗、遮阳帘、变色车顶、防夹检测等；","responsibility_in":null,"responsibility_out":null,"domain_group":"车身\n（1XX）","is_allocatable":true,"sort_order":5},{"code":"SS-007","name":"车身位置调节系统","description":"提供外后视镜、座椅、方向盘位置状态；\n控制后视镜、座椅、方向盘调节及记忆；","responsibility_in":null,"responsibility_out":null,"domain_group":"车身\n（1XX）","is_allocatable":true,"sort_order":6},{"code":"SS-008","name":"车身温度调节系统","description":"提供后视镜加热、后风挡加热、方向盘加热、座椅加热、座椅通风状态；\n控制后视镜加热、后风挡加热、方向盘加热、座椅加热通风；","responsibility_in":null,"responsibility_out":null,"domain_group":"车身\n（1XX）","is_allocatable":true,"sort_order":7},{"code":"SS-009","name":"车外声音提醒系统","description":"提供喇叭状态；\n控制喇叭鸣叫；\nAVAS状态及控制","responsibility_in":null,"responsibility_out":null,"domain_group":"车身\n（1XX）","is_allocatable":true,"sort_order":8},{"code":"SS-010","name":"被动安全系统","description":"提供安全带、座椅占位、碰撞状态；\n控制点火回路点爆气囊，记录碰撞数据；","responsibility_in":null,"responsibility_out":null,"domain_group":"车身\n（1XX）","is_allocatable":true,"sort_order":9},{"code":"SS-011","name":"低压电源管理系统","description":"提供电源模式、区域配电状态；\n控制低压电源模式切换及配电；","responsibility_in":null,"responsibility_out":null,"domain_group":"车身\n（1XX）","is_allocatable":true,"sort_order":10},{"code":"SS-012","name":"电动门系统","description":"提供电动尾门、门吸合锁状态\n控制电动尾门、电动侧滑门、电动侧门、吸合锁；检测防夹；","responsibility_in":null,"responsibility_out":null,"domain_group":"车身\n（1XX）","is_allocatable":true,"sort_order":11},{"code":"SS-013","name":"转向控制系统","description":"提供方向盘转角、方向盘手力矩状态\n控制车辆横向控制、横向控制辅助、接收智驾域横向控制需求","responsibility_in":null,"responsibility_out":null,"domain_group":"底盘\n（2XX）","is_allocatable":true,"sort_order":12},{"code":"SS-014","name":"制动控制系统","description":"提供车速、四轮轮速、紧急制动报警状态\n纵向减速控制、纵向控制辅助功能、制动能量回收、承接智驾域纵向控制请求","responsibility_in":null,"responsibility_out":null,"domain_group":"底盘\n（2XX）","is_allocatable":true,"sort_order":13},{"code":"SS-015","name":"驻车控制系统","description":"提供驻车EPB、转股模式、维修模式、工厂模式、洗车模式状态\n控制车辆驻车、接收智驾域和动力驻车请求","responsibility_in":null,"responsibility_out":null,"domain_group":"底盘\n（2XX）","is_allocatable":true,"sort_order":14},{"code":"SS-016","name":"胎压监测系统","description":"胎压监测及显示/胎压监测报警","responsibility_in":null,"responsibility_out":null,"domain_group":"底盘\n（2XX）","is_allocatable":true,"sort_order":15},{"code":"SS-017","name":"空气弹簧调节系统","description":"提供车身高度、车辆特殊模式状态\n控制车身高度调节","responsibility_in":null,"responsibility_out":null,"domain_group":"底盘\n（2XX）","is_allocatable":true,"sort_order":16},{"code":"SS-018","name":"减震器阻尼控制系统","description":"控制车辆减震器和阻尼器状态","responsibility_in":null,"responsibility_out":null,"domain_group":"底盘\n（2XX）","is_allocatable":true,"sort_order":17},{"code":"SS-019","name":"驾驶员显示系统","description":"提供给驾驶员功能故障提醒，状态显示等提醒信息，可显示在仪表或HUD上","responsibility_in":null,"responsibility_out":null,"domain_group":"信息娱乐（3XX）","is_allocatable":true,"sort_order":18},{"code":"SS-020","name":"用户输入系统","description":"提供方向盘按键、空调硬按键、车窗、危险报警灯、组合开关以及中控锁等整车硬按键用户操作输入","responsibility_in":null,"responsibility_out":null,"domain_group":"信息娱乐（3XX）","is_allocatable":true,"sort_order":19},{"code":"SS-021","name":"导航系统","description":"/","responsibility_in":null,"responsibility_out":null,"domain_group":"信息娱乐（3XX）","is_allocatable":true,"sort_order":20},{"code":"SS-022","name":"HMI交互及显示系统","description":"提供用户显示屏界面坐标输入、360、APS、行车记录仪、能量中心、手机互联、以及车控（空调 底盘 车身 热管理 ADAS 氛围灯 HUD等）、车模、OTA、E-Call等界面显示","responsibility_in":null,"responsibility_out":null,"domain_group":"信息娱乐（3XX）","is_allocatable":true,"sort_order":21},{"code":"SS-023","name":"娱乐系统","description":"提供本地收音机、蓝牙、USB、以及第三方在线音视频、游戏、新闻等等","responsibility_in":null,"responsibility_out":null,"domain_group":"信息娱乐（3XX）","is_allocatable":true,"sort_order":22},{"code":"SS-024","name":"个人中心系统","description":"提供账号建立、登录、绑定、记忆、退出以及注销流程，数字钥匙信息同步","responsibility_in":null,"responsibility_out":null,"domain_group":"信息娱乐（3XX）","is_allocatable":true,"sort_order":23},{"code":"SS-025","name":"语音系统","description":"/","responsibility_in":null,"responsibility_out":null,"domain_group":"信息娱乐（3XX）","is_allocatable":true,"sort_order":24},{"code":"SS-026","name":"IVI本地系统","description":"提供座舱IVI系统自身设置如系统版本号、显示屏亮度调节、时间、语言设、主题、屏保等设置，电话应用（蓝牙电话、微信电话、其他第三方电话），隐私政策，免责声明，系统升级。开机动画、首页、 状态栏、Dock栏、快捷中心，包括主副驾","responsibility_in":null,"responsibility_out":null,"domain_group":"信息娱乐（3XX）","is_allocatable":true,"sort_order":25},{"code":"SS-027","name":"舱内感知系统","description":"提供车内驾驶员、乘员以及活体物体等检测，动作识别，人脸识别，包括摄像头、车内红外光、雷达等车内检测传感器","responsibility_in":null,"responsibility_out":null,"domain_group":"信息娱乐（3XX）","is_allocatable":true,"sort_order":26},{"code":"SS-028","name":"行车记录仪系统","description":"行车记录仪设置、录像、存储，哨兵模式","responsibility_in":null,"responsibility_out":null,"domain_group":"信息娱乐（3XX）","is_allocatable":true,"sort_order":27},{"code":"SS-029","name":"车内声音系统","description":"定义车内音源定义、优先级定义；提供音效、音场以及音量调节等功能","responsibility_in":null,"responsibility_out":null,"domain_group":"信息娱乐（3XX）","is_allocatable":true,"sort_order":28},{"code":"SS-030","name":"外部设备链接系统","description":"包括手机互联连接、手表、U盘、WiFi、蓝牙、游戏手柄、k歌麦克风、无线充电等设备连接","responsibility_in":null,"responsibility_out":null,"domain_group":"信息娱乐（3XX）","is_allocatable":true,"sort_order":29},{"code":"SS-031","name":"远程服务系统","description":"驾驶员可以通过手机APP应用远程控车，查看监控车辆状态，国家平台上传信息，地方平台上传信息，企业平台上传信息","responsibility_in":null,"responsibility_out":null,"domain_group":"信息娱乐（3XX）","is_allocatable":true,"sort_order":30},{"code":"SS-032","name":"X-Call系统","description":"提供B-call E-call I-call功能","responsibility_in":null,"responsibility_out":null,"domain_group":"信息娱乐（3XX）","is_allocatable":true,"sort_order":31},{"code":"SS-033","name":"高压能量管理系统","description":"控制整车高压上下电、AC充电、AC放电、DC充电、高压能量分配、高压安全系统、高压使用场景下的仪表显示计算功能、高压电池管理等功能","responsibility_in":null,"responsibility_out":null,"domain_group":"动力\n（4XX）","is_allocatable":true,"sort_order":32},{"code":"SS-034","name":"低压能量管理系统","description":"控制动力域休眠唤醒、低压允许使用能量计算、DCDC控制需求等","responsibility_in":null,"responsibility_out":null,"domain_group":"动力\n（4XX）","is_allocatable":true,"sort_order":33},{"code":"SS-035","name":"挡位控制系统","description":"此功能包括换挡器请求控制，APS/ADAS换挡控制，自动换挡控制，EPB控制请求，以及挡位信息显示","responsibility_in":null,"responsibility_out":null,"domain_group":"动力\n（4XX）","is_allocatable":true,"sort_order":34},{"code":"SS-036","name":"驱动控制系统","description":"该功能根据车辆运行情况控制电机输出的转矩、速度和方向；转矩控制包括驾驶员转矩需求与ADAS、制动回收、底盘功能协调；此功能还包括电机模式控制、电驱内部相关控制需求；踏板解析、车速计算等","responsibility_in":null,"responsibility_out":null,"domain_group":"动力\n（4XX）","is_allocatable":true,"sort_order":35},{"code":"SS-037","name":"热管理系统","description":"空调系统的手动控制，自动控制，远程空调控制，乘员舱的制冷，制热，除霜除湿等功能，电池系统的冷却和加热，电池温度均衡，电驱系统的冷却，香氛控制","responsibility_in":null,"responsibility_out":null,"domain_group":"热管理（4XX）","is_allocatable":true,"sort_order":36},{"code":"SS-038","name":"感知融合和定位系统","description":"提供驾驶员和周边的环境信息，包括车道线、交通标志、光线、障碍物，距离、目标车车速，目标车方位、地理位置等等，同时也会提供各个传感器的状态。对于环境信息是实时的。","responsibility_in":null,"responsibility_out":null,"domain_group":"自动驾驶（5XX）","is_allocatable":true,"sort_order":37},{"code":"SS-039","name":"泊车控制系统","description":"系统集成了PKS(泊车碰撞告警)/APS（自动泊车）/RPA(遥控泊车)/AVP(自动代客泊车)/记忆泊车/AVM(360全景)/倒车循迹的核心算法，提供泊车控制下的转向/制动/扭矩请求/画面信息/报警请求","responsibility_in":null,"responsibility_out":null,"domain_group":"自动驾驶（5XX）","is_allocatable":true,"sort_order":38},{"code":"SS-040","name":"主动安全控制系统","description":"系统集成了驾驶辅助的核心算法，主要是对碰撞风险进行评估，并对响应动作进行仲裁，是该报警还是制动/转向。提供驾驶辅助下的转向/制动/扭矩/显示以及报警请求。\n系统包括如下逻辑模块：\n高级驾驶辅助：集成ACC（自适应巡航系统）/ALC（自动变道辅助）/HWA（高速公路辅助）/TJA（交通拥堵辅助）/高速NOA领航辅助/U-NOA(城市领航驾驶)/HWP(高速公路领航)/城市记忆领航/人机共驾的核心算法\n碰撞预警及制动：集成FCW/AMAP（加速踏板防误踩）/AEB（自动紧急制动系统）/FCTA（前向横穿交通警示系统）/FCTB（前向横穿交通制动系统）/RCTA（后向横穿交通警示系统）/RCTB（后向横穿交通制动系统）/RCW（后碰撞预警）的核心算法\n车道保持辅助：集成LKA(LDW（车道偏离预警）/LCC（车道居中控制）/LDP（车道偏离抑制）)/ELK（紧急车道保持）/EMA（转向避撞辅助）的核心算法\n驾驶行为辅助：集成LCA（变道辅助）/BSD（盲点监测系统）/DOW（门开预警）/TSR（交通标识识别）/ISA（速度限制辅助）/AHB（自适应远近光灯）/TLA（交通灯提醒）/行车图像采集/虚拟场景构建的核心算法","responsibility_in":null,"responsibility_out":null,"domain_group":"自动驾驶（5XX）","is_allocatable":true,"sort_order":39},{"code":"SS-041","name":"车辆模式系统","description":"提供正常模式、工厂模式、运输模式、展车模式、测功模式、节能模式、洗车模式、拖车模式","responsibility_in":null,"responsibility_out":null,"domain_group":"车辆模式\n（7XX）","is_allocatable":true,"sort_order":40},{"code":"SS-042","name":"场景编辑子系统","description":"用户通过交互界面编辑自定义场景，包括选择服务接口作为触发条件、执行动作等，保存成相应的场景配置信息；","responsibility_in":null,"responsibility_out":null,"domain_group":"SOA（8XX)","is_allocatable":true,"sort_order":41},{"code":"SS-043","name":"场景引擎子系统","description":"根据用户场景配置信息，实现用户自定义的场景功能，包括调用服务接口，获取服务数据、根据场景逻辑进行判断、下发服务指令数据等","responsibility_in":null,"responsibility_out":null,"domain_group":"SOA（8XX)","is_allocatable":true,"sort_order":42}]'::jsonb;
 digest constant text := 'aab5682575fb83ef44745f9c44c53cc6c918ad979efa4b435b76c4529a1c3c0b';
 template_name constant text := 'test-project-1-20261010-v1';
 current_design uuid;
 design_metadata jsonb;
 new_design uuid;
 new_subsystem uuid;
 item record;
 inserted integer := 0;
BEGIN
 IF session_user <> 'ee_project_api_rw' OR p_project IS NULL OR p_actor IS NULL OR nullif(btrim(p_request_id),'') IS NULL THEN
  RAISE EXCEPTION 'Invalid catalogue initialization caller' USING ERRCODE='42501';
 END IF;
 PERFORM 1 FROM ee.projects p WHERE p.id=p_project AND p.created_by=p_actor AND p.archived_at IS NULL FOR UPDATE;
 IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM ee.app_users WHERE id=p_actor AND state='active') OR
    NOT EXISTS(SELECT 1 FROM ee.project_members WHERE project_id=p_project AND user_id=p_actor AND state='active') THEN
  RAISE EXCEPTION 'Only the active creator can initialize this project' USING ERRCODE='42501';
 END IF;
 SELECT d.current_version_id,d.metadata INTO current_design,design_metadata
 FROM ee.project_subsystem_designs d WHERE d.project_id=p_project AND d.created_by=p_actor FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Expected creator-owned empty project scheme' USING ERRCODE='55000'; END IF;
 IF current_design IS NOT NULL THEN
  IF design_metadata->>'default_template_sha256'=digest AND
     (SELECT jsonb_agg(jsonb_build_object('code',s.code,'name',i.name,'description',i.description,
       'responsibility_in',i.responsibility_in,'responsibility_out',i.responsibility_out,'domain_group',i.domain_group,
       'is_allocatable',i.is_allocatable,'sort_order',i.sort_order) ORDER BY i.sort_order,s.code)
      FROM ee.subsystem_version_items i JOIN ee.subsystems s ON s.project_id=i.project_id AND s.id=i.subsystem_id
      WHERE i.project_id=p_project AND i.design_version_id=current_design)=template THEN
   RETURN QUERY SELECT current_design,jsonb_array_length(template),digest;
   RETURN;
  END IF;
  RAISE EXCEPTION 'Existing catalogue will not be overwritten' USING ERRCODE='55000';
 END IF;
 IF EXISTS(SELECT 1 FROM ee.subsystems WHERE project_id=p_project) OR
    EXISTS(SELECT 1 FROM ee.subsystem_design_versions WHERE project_id=p_project) OR
    EXISTS(SELECT 1 FROM ee.subsystem_version_items WHERE project_id=p_project) THEN
  RAISE EXCEPTION 'Partial or historical catalogue will not be overwritten' USING ERRCODE='55000';
 END IF;
 new_design := gen_random_uuid();
 INSERT INTO ee.subsystem_design_versions(id,project_id,version_no,change_summary,created_via,metadata,created_by)
 VALUES(new_design,p_project,1,'Default catalogue: '||template_name,'copy',jsonb_build_object('default_template_version',template_name,'default_template_sha256',digest),p_actor);
 FOR item IN SELECT * FROM jsonb_to_recordset(template) AS t(code text,name text,description text,responsibility_in text,responsibility_out text,domain_group text,is_allocatable boolean,sort_order integer)
 LOOP
  new_subsystem := gen_random_uuid();
  INSERT INTO ee.subsystems(id,project_id,code,created_by) VALUES(new_subsystem,p_project,item.code,p_actor);
  INSERT INTO ee.subsystem_version_items(project_id,design_version_id,subsystem_id,name,description,responsibility_in,responsibility_out,domain_group,is_allocatable,sort_order,source_metadata)
  VALUES(p_project,new_design,new_subsystem,item.name,item.description,item.responsibility_in,item.responsibility_out,item.domain_group,item.is_allocatable,item.sort_order,jsonb_build_object('default_template_version',template_name,'template_code',item.code));
  inserted := inserted+1;
 END LOOP;
 IF inserted <> 43 THEN RAISE EXCEPTION 'Default catalogue count mismatch'; END IF;
 UPDATE ee.project_subsystem_designs SET current_version_id=new_design,lock_version=lock_version+1,
 metadata=metadata||jsonb_build_object('default_template_version',template_name,'default_template_sha256',digest),updated_by=p_actor
 WHERE project_id=p_project;
 INSERT INTO ee.security_audit_events(id,project_id,actor_id,subject_user_id,event_type,scope_kind,after_state,reason,request_id)
 VALUES(gen_random_uuid(),p_project,p_actor,p_actor,'project.subsystems.initialize','project',
        jsonb_build_object('design_version_id',new_design,'count',inserted,'template_version',template_name,'template_sha256',digest),
        'Initialize fixed default catalogue only; no requirements or allocations copied',p_request_id);
 RETURN QUERY SELECT new_design,inserted,digest;
END $body$;
ALTER FUNCTION ee.initialize_default_subsystems_v1(uuid,uuid,text) OWNER TO ee_m0_owner;
REVOKE ALL ON FUNCTION ee.initialize_default_subsystems_v1(uuid,uuid,text) FROM PUBLIC,ee_project_api_ro;
GRANT EXECUTE ON FUNCTION ee.initialize_default_subsystems_v1(uuid,uuid,text) TO ee_project_api_rw;
COMMENT ON FUNCTION ee.initialize_default_subsystems_v1(uuid,uuid,text) IS 'default-subsystems-v1 sha256=aab5682575fb83ef44745f9c44c53cc6c918ad979efa4b435b76c4529a1c3c0b';
DO $verify$
DECLARE f record;
BEGIN
 SELECT p.* INTO f FROM pg_proc p WHERE p.oid=to_regprocedure('ee.initialize_default_subsystems_v1(uuid,uuid,text)');
 IF NOT FOUND THEN RAISE EXCEPTION 'Default subsystem initializer is not installed'; END IF;
 IF pg_get_userbyid(f.proowner)<>'ee_m0_owner' OR NOT f.prosecdef OR
    f.proconfig IS DISTINCT FROM ARRAY['search_path=pg_catalog, ee, pg_temp'] OR
    encode(sha256(convert_to(f.prosrc,'UTF8')),'hex')<>'3df5defee6feba6f94b626bdf2a6df0c142547fc6a1a3604aa96a214f8c4bfe4' THEN
  RAISE EXCEPTION 'Initializer definition or security metadata mismatch';
 END IF;
 IF EXISTS(SELECT 1 FROM aclexplode(coalesce(f.proacl,acldefault('f',f.proowner))) a
           WHERE a.grantee NOT IN(f.proowner,(SELECT oid FROM pg_roles WHERE rolname='ee_project_api_rw'))
              OR a.privilege_type<>'EXECUTE' OR (a.grantee<>f.proowner AND a.is_grantable)) OR
    NOT has_function_privilege('ee_project_api_rw',f.oid,'EXECUTE') OR
    has_function_privilege('ee_project_api_ro',f.oid,'EXECUTE') THEN
  RAISE EXCEPTION 'Initializer execute privileges mismatch';
 END IF;
 IF EXISTS(SELECT 1 FROM unnest(ARRAY['ee.subsystems','ee.subsystem_design_versions','ee.subsystem_version_items']) t(name)
  WHERE has_table_privilege('ee_project_api_rw',name,'INSERT,UPDATE,DELETE')) OR
  has_column_privilege('ee_project_api_rw','ee.project_subsystem_designs','current_version_id','UPDATE') THEN
  RAISE EXCEPTION 'Unexpected broad catalogue write permissions';
 END IF;
END $verify$;
COMMIT;
