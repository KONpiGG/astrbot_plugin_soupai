import json
import asyncio
import os
from datetime import datetime, time
from typing import Dict, Optional, Tuple, List
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import LLMResponse
from astrbot.api import logger, AstrBotConfig
from astrbot.core.utils.session_waiter import session_waiter, SessionController, SessionFilter
from astrbot.api.message_components import At


# 游戏状态管理
class GameState:
    def __init__(self):
        self.active_games: Dict[str, Dict] = {}  # 群聊ID -> 游戏状态
    
    def start_game(self, group_id: str, puzzle: str, answer: str) -> bool:
        """开始游戏，返回是否成功"""
        if group_id in self.active_games:
            return False
        self.active_games[group_id] = {
            "puzzle": puzzle,
            "answer": answer,
            "is_active": True
        }
        return True
    
    def end_game(self, group_id: str) -> bool:
        """结束游戏"""
        if group_id in self.active_games:
            del self.active_games[group_id]
            return True
        return False
    
    def get_game(self, group_id: str) -> Optional[Dict]:
        """获取游戏状态"""
        return self.active_games.get(group_id)
    
    def is_game_active(self, group_id: str) -> bool:
        """检查是否有活跃游戏"""
        return group_id in self.active_games


# 存储库管理
class StoryStorage:
    def __init__(self, storage_file: str, max_size: int = 50):
        self.storage_file = storage_file
        self.max_size = max_size
        self.stories: List[Dict] = []
        self.load_stories()
    
    def load_stories(self):
        """从文件加载故事"""
        try:
            if os.path.exists(self.storage_file):
                with open(self.storage_file, 'r', encoding='utf-8') as f:
                    self.stories = json.load(f)
                logger.info(f"从 {self.storage_file} 加载了 {len(self.stories)} 个故事")
            else:
                self.stories = []
                logger.info("存储库文件不存在，创建新的存储库")
        except Exception as e:
            logger.error(f"加载故事失败: {e}")
            self.stories = []
    
    def save_stories(self):
        """保存故事到文件"""
        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(self.storage_file), exist_ok=True)
            with open(self.storage_file, 'w', encoding='utf-8') as f:
                json.dump(self.stories, f, ensure_ascii=False, indent=2)
            logger.info(f"保存了 {len(self.stories)} 个故事到 {self.storage_file}")
        except Exception as e:
            logger.error(f"保存故事失败: {e}")
    
    def add_story(self, puzzle: str, answer: str) -> bool:
        """添加故事到存储库"""
        if len(self.stories) >= self.max_size:
            # 移除最旧的故事
            self.stories.pop(0)
            logger.info("存储库已满，移除最旧的故事")
        
        story = {
            "puzzle": puzzle,
            "answer": answer,
            "created_at": datetime.now().isoformat()
        }
        self.stories.append(story)
        self.save_stories()
        logger.info(f"添加新故事到存储库，当前存储库大小: {len(self.stories)}")
        return True
    
    def get_story(self) -> Optional[Tuple[str, str]]:
        """从存储库获取一个故事"""
        if not self.stories:
            return None
        
        story = self.stories.pop(0)  # 移除并返回第一个故事
        self.save_stories()
        logger.info(f"从存储库获取故事，剩余: {len(self.stories)}")
        return story["puzzle"], story["answer"]
    
    def get_storage_info(self) -> Dict:
        """获取存储库信息"""
        return {
            "total": len(self.stories),
            "max_size": self.max_size,
            "available": self.max_size - len(self.stories)
        }


# 验证结果类
class VerificationResult:
    """验证结果类"""
    def __init__(self, level: str, comment: str, is_correct: bool = False):
        self.level = level
        self.comment = comment
        self.is_correct = is_correct
    
    def to_dict(self) -> Dict:
        return {
            "level": self.level,
            "comment": self.comment,
            "is_correct": self.is_correct
        }


# 自定义会话过滤器 - 以群为单位进行会话控制
class GroupSessionFilter(SessionFilter):
    def filter(self, event: AstrMessageEvent) -> str:
        return event.get_group_id() if event.get_group_id() else event.unified_msg_origin


@register("soupai", "KONpiGG", "AI 海龟汤推理游戏插件", "1.0.0", "https://github.com/KONpiGG/astrbot_plugin_soupai")
class SoupaiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.game_state = GameState()
        
        # 获取配置值
        self.generate_llm_provider_id = self.config.get("generate_llm_provider", "")
        self.judge_llm_provider_id = self.config.get("judge_llm_provider", "")
        self.game_timeout = self.config.get("game_timeout", 300)
        self.storage_max_size = self.config.get("storage_max_size", 50)
        self.auto_generate_start = self.config.get("auto_generate_start", 3)
        self.auto_generate_end = self.config.get("auto_generate_end", 6)
        
        # 初始化存储库 - 使用 AstrBot 的 data 目录，确保数据持久化
        data_dir = os.path.join("data", "plugins", "soupai")
        os.makedirs(data_dir, exist_ok=True)
        storage_file = os.path.join(data_dir, "soupai_stories.json")
        self.story_storage = StoryStorage(storage_file, self.storage_max_size)
        
        # 防止重复调用的状态
        self.generating_games = set()  # 正在生成谜题的群聊ID集合
        
        # 自动生成状态
        self.auto_generating = False
        self.auto_generate_task = None
        
        # 启动自动生成任务
        asyncio.create_task(self._start_auto_generate())
        
        logger.info(f"海龟汤插件已加载，配置: 生成LLM提供商={self.generate_llm_provider_id}, 判断LLM提供商={self.judge_llm_provider_id}, 超时时间={self.game_timeout}秒, 存储库大小={self.storage_max_size}")

    async def terminate(self):
        """插件卸载时清理资源"""
        # 停止自动生成
        self.auto_generating = False
        if self.auto_generate_task:
            self.auto_generate_task.cancel()
        logger.info("海龟汤插件已卸载呜呜呜呜")

    async def _start_auto_generate(self):
        """启动自动生成任务"""
        while True:
            try:
                now = datetime.now()
                current_hour = now.hour
                
                # 检查是否在自动生成时间范围内
                if self.auto_generate_start <= current_hour < self.auto_generate_end:
                    if not self.auto_generating:
                        logger.info(f"开始自动生成故事，时间: {current_hour}:00")
                        self.auto_generating = True
                        asyncio.create_task(self._auto_generate_loop())
                else:
                    if self.auto_generating:
                        logger.info(f"停止自动生成故事，时间: {current_hour}:00")
                        self.auto_generating = False
                
                # 等待1小时后再次检查
                await asyncio.sleep(3600)  # 1小时
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"自动生成任务错误: {e}")
                await asyncio.sleep(3600)  # 出错后等待1小时再试

    async def _auto_generate_loop(self):
        """自动生成循环"""
        while self.auto_generating:
            try:
                # 检查存储库是否已满
                storage_info = self.story_storage.get_storage_info()
                if storage_info["available"] <= 0:
                    logger.info("存储库已满，停止自动生成")
                    break
                
                # 生成一个故事
                puzzle, answer = await self.generate_story_with_llm()
                if puzzle and answer and not puzzle.startswith("（"):
                    self.story_storage.add_story(puzzle, answer)
                    logger.info("自动生成故事成功")
                else:
                    logger.warning("自动生成故事失败")
                
                # 等待5分钟再生成下一个
                await asyncio.sleep(300)  # 5分钟
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"自动生成故事错误: {e}")
                await asyncio.sleep(300)  # 出错后等待5分钟再试

    # ✅ 生成谜题和答案
    async def generate_story_with_llm(self) -> Tuple[str, str]:
        """使用 LLM 生成海龟汤谜题"""
        print(f"[测试输出] 开始生成故事，LLM提供商ID: {self.generate_llm_provider_id}")
        
        # 根据配置获取指定的生成 LLM 提供商
        if self.generate_llm_provider_id:
            provider = self.context.get_provider_by_id(self.generate_llm_provider_id)
            if provider is None:
                logger.error(f"未找到指定的生成 LLM 提供商: {self.generate_llm_provider_id}")
                print(f"[测试输出] 生成故事失败：未找到指定的LLM提供商 {self.generate_llm_provider_id}")
                return "（无法生成题面，指定的生成 LLM 提供商不存在）", "（无）"
        else:
            provider = self.context.get_using_provider()
            if provider is None:
                logger.error("未配置 LLM 服务商")
                print(f"[测试输出] 生成故事失败：未配置LLM服务商")
                return "（无法生成题面，请先配置大语言模型）", "（无）"

        prompt = self._build_puzzle_prompt()

        try:
            logger.info("开始调用 LLM 生成谜题...")
            print(f"[测试输出] 调用LLM生成谜题...")
            llm_resp: LLMResponse = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                func_tool=None,
                image_urls=[],
                system_prompt="你是一个专业的反转推理谜题创作者，专门为海龟汤游戏设计谜题。你需要创作简洁、具象、有逻辑反转的谜题，让玩家能够通过是/否提问逐步还原真相。每次创作都必须全新、原创，不能重复已有故事。"
            )

            text = llm_resp.completion_text.strip()
            logger.info(f"LLM 返回内容: {text}")
            print(f"[测试输出] LLM返回内容: {text[:100]}...")
            
            # 尝试多种格式解析
            puzzle = None
            answer = None
            
            # 格式1: "题面：xxx 答案：xxx"
            if "题面：" in text and "答案：" in text:
                puzzle = text.split("题面：")[1].split("答案：")[0].strip()
                answer = text.split("答案：")[1].strip()
            
            # 格式2: "**题面**：xxx **答案**：xxx" (Markdown格式)
            elif "**题面**" in text and "**答案**" in text:
                puzzle = text.split("**题面**")[1].split("**答案**")[0].strip()
                if puzzle.startswith("：") or puzzle.startswith(":"):
                    puzzle = puzzle[1:].strip()
                answer = text.split("**答案**")[1].strip()
                if answer.startswith("：") or answer.startswith(":"):
                    answer = answer[1:].strip()
            
            # 格式3: "题面：xxx\n答案：xxx"
            elif "题面：" in text and "\n答案：" in text:
                puzzle = text.split("题面：")[1].split("\n答案：")[0].strip()
                answer = text.split("\n答案：")[1].strip()
            
            # 格式4: 尝试从文本中提取题面和答案
            else:
                lines = text.split('\n')
                for i, line in enumerate(lines):
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    
                    # 寻找题面
                    if not puzzle and ('题面' in line or '**题面**' in line):
                        puzzle = line
                        if '：' in line:
                            puzzle = line.split('：', 1)[1].strip()
                        elif ':' in line:
                            puzzle = line.split(':', 1)[1].strip()
                        # 移除可能的Markdown标记
                        puzzle = puzzle.replace('**', '').replace('*', '').strip()
                    
                    # 寻找答案
                    elif not answer and ('答案' in line or '**答案**' in line):
                        answer = line
                        if '：' in line:
                            answer = line.split('：', 1)[1].strip()
                        elif ':' in line:
                            answer = line.split(':', 1)[1].strip()
                        # 移除可能的Markdown标记
                        answer = answer.replace('**', '').replace('*', '').strip()
                    
                    # 如果找到了题面但还没找到答案，继续寻找
                    elif puzzle and not answer and len(line) > 20:
                        # 可能是答案的开始
                        answer = line
            
            if puzzle and answer:
                # 清理答案中的多余内容
                if "----" in answer:
                    answer = answer.split("----")[0].strip()
                if "---" in answer:
                    answer = answer.split("---")[0].strip()
                
                logger.info(f"成功解析谜题: 题面='{puzzle}', 答案='{answer}'")
                print(f"[测试输出] 成功解析谜题: 题面='{puzzle}', 答案='{answer[:50]}...'")
                return puzzle, answer

            logger.error(f"LLM 返回内容格式错误: {text}")
            print(f"[测试输出] LLM返回内容格式错误: {text[:100]}...")
            return "生成失败", "无法解析 LLM 返回的内容"
        except Exception as e:
            logger.error(f"生成谜题失败: {e}")
            print(f"[测试输出] 生成谜题异常: {e}")
            return "生成失败", f"LLM 调用出错: {e}"

    def _build_puzzle_prompt(self) -> str:
        """构建谜题生成的提示词"""
        import random
        
        # 丰富的主题列表，增加多样性
        themes = [
            # 🔍 人类行为与误导
            "误解他人行为的代价",
            "看似反常实则合理的选择",
            "主动伪装带来的反转",
            "隐瞒真相与道德困境",
            "他人为主角设下的圈套",
            "故意失败的计划",
            "真实动机被遮蔽",
            "道德与规则的冲突",

            # 🧠 心理博弈与控制
            "陷害与自保之间的抉择",
            "信息不对称引发的误判",
            "操控他人感知的行为",
            "主观偏见导致的误解",
            "冷静外表下的激烈动机",
            "以退为进的心理策略",

            # 🧪 现实逻辑与错觉
            "空间结构引发的错觉",
            "物品使用的误导性",
            "因果顺序的错配",
            "隐藏在日常中的意外用途",
            "非典型证据的误导",
            "时间线的巧妙安排",

            # 📍 社会环境与冲突
            "职场中的暗中博弈",
            "公众场合下的隐秘行为",
            "权力结构下的自我保护",
            "日常制度漏洞的利用",
            "面对规则边缘的选择",
            "技术被滥用的后果",
            "资源争夺下的灰色行为",

            # 🧩 特定身份与角色
            "保安不是最了解监控的人",
            "程序员的删除并非错误",
            "清洁工的观察比谁都细致",
            "老师的行为引发质疑",
            "医生做出的不寻常选择",
            "司机的路线似乎有问题",
            "演员的自毁是否另有用意",

            # 🕯 情感错位与人性
            "好意引发的巨大误会",
            "爱被误解为恶意",
            "习惯性行为暴露了真相",
            "为了他人不得不说谎",
            "逃避责任的精心设计",
            "牺牲某人换取整体安全"
        ]
        
        selected_theme = random.choice(themes)
        
        prompt = (
            f"你是一个逻辑推理谜题设计师，正在创作一个用于【海龟汤游戏】的原创谜题。\n\n"
            "【目标】：生成一个结构清晰、信息复杂、具备反差感的逻辑谜题，玩家可以通过是/否提问逐步还原真相。答案中解释的所有行为和结果，必须都在题面中有所体现或留有暗示，禁止引入题面未提及的核心行为或结果。谜题在满足以上要求的前提下，应尽可能风格多样、身份多样、行为设定独特、反转机制不重复，避免模板化创作。\n\n"
            "【题面】要求：\n"
            "1~2句话，控制在30字以内，但不能过短或单一；\n"
            "必须包含具体人物 + 至少两个具体细节或行为（如行为+环境、行为+结果、两个动作等）；\n"
            "行为必须具象明确，严禁使用抽象词、形容词、心理或情绪描述；\n"
            "必须包含异常或矛盾要素，能引发为什么？的思考；\n"
            "允许黑暗元素，如陷害、伤害、诱导、自残、掩盖证据等冷峻现实情节；\n"
            "不得使用幻想、梦境、魔法、精神病等设定；\n"
            "使用陈述句，不得使用疑问句或解释语气。\n\n"
            "【答案】要求：\n"
            "不超过200字；\n"
            "真实可实现，具有完整因果逻辑；\n"
            "至少包含两个推理层次或误导点（例如动机误导+情境误导）；\n"
            "不得出现反转在于、真相是、实际上之类的总结或解释语；\n"
            "不要使用说明性句子或教学语气；\n"
            "整体氛围可偏冷峻，但必须具备可还原性，逻辑自洽。\n"
            "答案仅用于解释题面中已有行为与结果，禁止引入题面未包含的额外关键事件或角色。\n\n"
            "参考例子：\n"
            "题面：女演员在试镜前剪断了自己的裙子，却最终被录取。\n"
            "答案：这名女演员事先得知试镜剧本中有一幕裙子被撕裂的情节。她故意提前剪开裙子并精心处理切口，使在表演时裙子自然裂开看起来逼真震撼。评审认为她的表演最具冲击力，毫不犹豫录取了她。她的破坏行为反而让她脱颖而出。\n\n"
            "【输出格式】：\n"
            "题面：XXX\n"
            "答案：XXX\n\n"
            f"请基于「{selected_theme}」主题生成一个完全原创的反转推理谜题。"
        )
        
        return prompt

    async def _generate_for_storage(self) -> bool:
        """为存储库生成故事"""
        try:
            puzzle, answer = await self.generate_story_with_llm()
            if puzzle and answer and not puzzle.startswith("（"):
                self.story_storage.add_story(puzzle, answer)
                logger.info("为存储库生成故事成功")
                return True
            else:
                logger.warning("为存储库生成故事失败")
                return False
        except Exception as e:
            logger.error(f"为存储库生成故事错误: {e}")
            return False

    # ✅ 验证用户推理
    async def verify_user_guess(self, user_guess: str, true_answer: str) -> VerificationResult:
        """
        验证用户推理
        
        Args:
            user_guess: 用户的推理内容
            true_answer: 标准答案
            
        Returns:
            VerificationResult: 验证结果
        """
        # 获取判断 LLM 提供商
        if self.judge_llm_provider_id:
            provider = self.context.get_provider_by_id(self.judge_llm_provider_id)
            if provider is None:
                logger.error(f"未找到指定的判断 LLM 提供商: {self.judge_llm_provider_id}")
                return VerificationResult("验证失败", "未配置判断 LLM，无法验证")
        else:
            provider = self.context.get_using_provider()
            if provider is None:
                return VerificationResult("验证失败", "未配置 LLM，无法验证")
        
        # 构建验证提示词
        system_prompt = self._build_verification_system_prompt()
        user_prompt = self._build_verification_user_prompt(user_guess, true_answer)
        
        try:
            logger.info(f"开始验证用户推理: '{user_guess[:50]}...'")
            
            llm_resp: LLMResponse = await provider.text_chat(
                prompt=user_prompt,
                contexts=[],
                func_tool=None,
                image_urls=[],
                system_prompt=system_prompt
            )
            
            text = llm_resp.completion_text.strip()
            logger.info(f"验证 LLM 返回内容: {text}")
            
            # 解析验证结果
            result = self._parse_verification_result(text)
            return result
            
        except Exception as e:
            logger.error(f"验证用户推理失败: {e}")
            return VerificationResult("验证失败", f"验证过程中发生错误: {e}")

    def _build_verification_system_prompt(self) -> str:
        """构建验证系统提示词"""
        return """你是一个推理游戏的裁判。玩家需要还原一个隐藏的完整故事，你的任务是根据玩家的陈述与标准答案对比，判断其相似程度。

请将玩家的陈述分为以下四个等级之一：

1. 完全还原：玩家推理与标准答案高度一致，包括核心逻辑与关键细节；
2. 核心推理正确：推理的大方向或关键反转已覆盖，但部分细节或次要原因有出入；
3. 部分正确：玩家理解了部分情节或动机，但因果链不完整或解释偏离较大；
4. 基本不符：玩家的推理与真相严重不符，没有合理解释故事中的矛盾。

请输出以下格式：
等级：{等级}
评价：{一句简评}

注意：
- 当等级为"完全还原"或"核心推理正确"时，表示玩家基本猜中了故事真相
- 评价应该简洁明了，指出玩家的推理优点和不足
- 只输出等级和评价，不要添加其他内容"""

    def _build_verification_user_prompt(self, user_guess: str, true_answer: str) -> str:
        """构建验证用户提示词"""
        return f"""标准答案是：
{true_answer}

玩家还原的推理是：
{user_guess}

请判断其等级和简评。"""

    def _parse_verification_result(self, text: str) -> VerificationResult:
        """解析验证结果"""
        try:
            # 提取等级和评价
            lines = text.strip().split('\n')
            level = ""
            comment = ""
            
            for line in lines:
                line = line.strip()
                if line.startswith("等级："):
                    level = line.replace("等级：", "").strip()
                elif line.startswith("评价："):
                    comment = line.replace("评价：", "").strip()
            
            # 判断是否猜中
            is_correct = level in ["完全还原", "核心推理正确"]
            
            if not level or not comment:
                # 如果解析失败，尝试从文本中提取信息
                if "完全还原" in text or "核心推理正确" in text:
                    level = "核心推理正确" if "核心推理正确" in text else "完全还原"
                    comment = "推理基本正确，但解析结果格式异常"
                    is_correct = True
                else:
                    level = "验证失败"
                    comment = "无法解析验证结果"
                    is_correct = False
            
            return VerificationResult(level, comment, is_correct)
            
        except Exception as e:
            logger.error(f"解析验证结果失败: {e}")
            return VerificationResult("验证失败", f"解析验证结果时发生错误: {e}")



    # ✅ 判断提问的回答方式
    async def judge_question(self, question: str, true_answer: str) -> str:
        """使用 LLM 判断用户提问的回答方式"""
        print(f"[测试输出] 开始判断问题: '{question[:30]}...'")
        
        # 根据配置获取指定的判断 LLM 提供商
        if self.judge_llm_provider_id:
            provider = self.context.get_provider_by_id(self.judge_llm_provider_id)
            if provider is None:
                logger.error(f"未找到指定的判断 LLM 提供商: {self.judge_llm_provider_id}")
                print(f"[测试输出] 判断问题失败：未找到指定的LLM提供商 {self.judge_llm_provider_id}")
                return "（未配置判断 LLM，无法判断）"
        else:
            provider = self.context.get_using_provider()
            if provider is None:
                print(f"[测试输出] 判断问题失败：未配置LLM服务商")
                return "（未配置 LLM，无法判断）"

        prompt = (
            f"海龟汤游戏规则：\n"
            f"1. 故事的完整真相是：{true_answer}\n"
            f"2. 玩家提问或陈述：\"{question}\"\n"
            f"3. 请判断玩家的说法是否符合真相\n"
            f"4. 只能回答：\"是\"、\"否\"或\"是也不是\"\n"
            f"5. \"是\"：完全符合真相\n"
            f"6. \"否\"：完全不符合真相\n"
            f"7. \"是也不是\"：部分符合或模糊不清\n\n"
            f"请根据以上规则判断并回答。"
        )

        try:
            print(f"[测试输出] 调用LLM判断问题...")
            llm_resp: LLMResponse = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                func_tool=None,
                image_urls=[],
                system_prompt="你是一个海龟汤推理游戏的助手。你必须严格按照游戏规则回答，只能回答\"是\"、\"否\"或\"是也不是\"，不能添加任何其他内容。"
            )

            reply = llm_resp.completion_text.strip()
            print(f"[测试输出] LLM判断回复: '{reply}'")
            if reply.startswith("是") or reply.startswith("否"):
                return reply
            return "是也不是。"
        except Exception as e:
            logger.error(f"判断问题失败: {e}")
            print(f"[测试输出] 判断问题异常: {e}")
            return "（判断失败，请重试）"

    # 🎮 开始游戏指令
    @filter.command("汤")
    async def start_soupai_game(self, event: AstrMessageEvent):
        """开始海龟汤游戏"""
        group_id = event.get_group_id()
        logger.info(f"收到开始游戏指令，群ID: {group_id}")
        print(f"[测试输出] 收到 /汤 指令，群ID: {group_id}")
        
        if not group_id:
            print("[测试输出] /汤 指令：非群聊环境，拒绝执行")
            yield event.plain_result("海龟汤游戏只能在群聊中进行哦~")
            return

        # 检查是否已有活跃游戏
        if self.game_state.is_game_active(group_id):
            logger.info(f"群 {group_id} 已有活跃游戏")
            print(f"[测试输出] /汤 指令：群 {group_id} 已有活跃游戏")
            yield event.plain_result("当前群聊已有活跃的海龟汤游戏，请等待游戏结束或使用 /揭晓 结束当前游戏。")
            return

        # 检查是否正在生成谜题
        if group_id in self.generating_games:
            logger.info(f"群 {group_id} 正在生成谜题，忽略重复请求")
            print(f"[测试输出] /汤 指令：群 {group_id} 正在生成谜题")
            yield event.plain_result("当前有正在生成的谜题，请稍候...")
            return

        try:
            # 标记正在生成谜题
            self.generating_games.add(group_id)
            logger.info(f"开始为群 {group_id} 生成谜题")
            print(f"[测试输出] /汤 指令：开始为群 {group_id} 生成谜题")
            
            # 优先从存储库获取故事
            story = self.story_storage.get_story()
            if story:
                puzzle, answer = story
                logger.info(f"从存储库获取故事，剩余: {self.story_storage.get_storage_info()['total']}")
                print(f"[测试输出] /汤 指令：从存储库获取故事成功，剩余: {self.story_storage.get_storage_info()['total']}")
                
                # 开始游戏
                if self.game_state.start_game(group_id, puzzle, answer):
                    print(f"[测试输出] /汤 指令：游戏启动成功，群ID: {group_id}")
                    yield event.plain_result(f"🎮 海龟汤游戏开始！\n\n📖 题面：{puzzle}\n\n💡 请直接提问或陈述，我会回答：是、否、是也不是\n💡 输入 /揭晓 可以查看完整故事")
                    
                    # 启动会话控制
                    await self._start_game_session(event, group_id, answer)
                else:
                    print(f"[测试输出] /汤 指令：游戏启动失败，群ID: {group_id}")
                    yield event.plain_result("游戏启动失败，请重试")
                
                # 移除生成状态，因为故事已经准备完成
                self.generating_games.discard(group_id)
                logger.info(f"群 {group_id} 故事准备完成，移除生成状态")
                print(f"[测试输出] /汤 指令：故事准备完成，移除生成状态，群ID: {group_id}")
                return
            
            # 存储库为空，现场生成
            print(f"[测试输出] /汤 指令：存储库为空，开始现场生成谜题")
            yield event.plain_result("正在生成海龟汤谜题，请稍候...")
            
            # 生成谜题
            puzzle, answer = await self.generate_story_with_llm()
            print(f"[测试输出] /汤 指令：现场生成谜题结果 - 题面: {puzzle[:20]}..., 答案: {answer[:20]}...")
            
            if puzzle == "（无法生成题面，请先配置大语言模型）":
                print(f"[测试输出] /汤 指令：生成谜题失败 - {answer}")
                yield event.plain_result(f"生成谜题失败：{answer}")
                # 生成失败时也要移除生成状态
                self.generating_games.discard(group_id)
                logger.info(f"群 {group_id} 生成失败，移除生成状态")
                print(f"[测试输出] /汤 指令：生成失败，移除生成状态，群ID: {group_id}")
                return

            # 开始游戏
            if self.game_state.start_game(group_id, puzzle, answer):
                print(f"[测试输出] /汤 指令：游戏启动成功，群ID: {group_id}")
                yield event.plain_result(f"🎮 海龟汤游戏开始！\n\n📖 题面：{puzzle}\n\n💡 请直接提问或陈述，我会回答：是、否、是也不是\n💡 输入 /揭晓 可以查看完整故事")
                
                # 启动会话控制
                await self._start_game_session(event, group_id, answer)
            else:
                print(f"[测试输出] /汤 指令：游戏启动失败，群ID: {group_id}")
                yield event.plain_result("游戏启动失败，请重试")
            
            # 移除生成状态，因为故事已经准备完成
            self.generating_games.discard(group_id)
            logger.info(f"群 {group_id} 故事准备完成，移除生成状态")
            print(f"[测试输出] /汤 指令：故事准备完成，移除生成状态，群ID: {group_id}")

        except Exception as e:
            logger.error(f"启动游戏失败: {e}")
            print(f"[测试输出] /汤 指令：启动游戏异常 - {e}")
            # 发生异常时也要移除生成状态
            self.generating_games.discard(group_id)
            logger.info(f"群 {group_id} 启动游戏异常，移除生成状态")
            print(f"[测试输出] /汤 指令：启动游戏异常，移除生成状态，群ID: {group_id}")
            yield event.plain_result(f"启动游戏时发生错误：{e}")

    # 🔍 揭晓指令
    @filter.command("揭晓")
    async def reveal_answer(self, event: AstrMessageEvent):
        """揭晓答案"""
        print(f"[测试输出] /揭晓 指令处理器被调用！")
        print(f"[测试输出] /揭晓 指令：完整消息: '{event.message_str}'")
        group_id = event.get_group_id()
        print(f"[测试输出] 收到 /揭晓 指令，群ID: {group_id}")
        
        if not group_id:
            print("[测试输出] /揭晓 指令：非群聊环境，拒绝执行")
            yield event.plain_result("海龟汤游戏只能在群聊中进行哦~")
            return

        # 检查是否有活跃游戏，如果有活跃游戏，说明在会话控制中，不在这里处理
        if self.game_state.is_game_active(group_id):
            print(f"[测试输出] /揭晓 指令：群 {group_id} 有活跃游戏，由会话控制处理，阻止事件传播")
            # 阻止事件继续传播，避免被会话控制系统重复处理
            await event.block()
            return
        else:
            print(f"[测试输出] /揭晓 指令：群 {group_id} 没有活跃游戏，独立处理器处理")

        game = self.game_state.get_game(group_id)
        if not game:
            print(f"[测试输出] /揭晓 指令：群 {group_id} 没有活跃游戏")
            yield event.plain_result("当前没有活跃的海龟汤游戏，请使用 /汤 开始新游戏。")
            return

        answer = game["answer"]
        puzzle = game["puzzle"]
        print(f"[测试输出] /揭晓 指令：揭晓答案成功，群ID: {group_id}")
        
        # 发送完整的揭晓信息
        yield event.plain_result(f"🎯 海龟汤游戏结束！\n\n📖 题面：{puzzle}\n📖 完整故事：{answer}\n\n感谢参与游戏！")
        
        # 结束游戏
        self.game_state.end_game(group_id)
        logger.info(f"游戏已结束，群ID: {group_id}")
        print(f"[测试输出] /揭晓 指令：游戏已结束，群ID: {group_id}")

    # 🎯 游戏会话控制
    async def _start_game_session(self, event: AstrMessageEvent, group_id: str, answer: str):
        """启动游戏会话控制"""
        try:
            @session_waiter(timeout=self.game_timeout, record_history_chains=False)
            async def game_session_waiter(controller: SessionController, event: AstrMessageEvent):
                try:
                    # 从游戏状态获取答案，确保变量可用
                    game = self.game_state.get_game(group_id)
                    if not game:
                        print(f"[测试输出] 会话控制：无法获取游戏状态，群ID: {group_id}")
                        return
                    current_answer = game["answer"]
                    user_input = event.message_str.strip()
                    logger.info(f"会话控制收到消息: '{user_input}'")
                    print(f"[测试输出] 会话控制收到消息: '{user_input}'")
                    print(f"[测试输出] 会话控制：原始消息: '{event.message_str}'")
                    print(f"[测试输出] 会话控制：消息类型: {type(event).__name__}")
                    print(f"[测试输出] 会话控制：消息来源: {event.unified_msg_origin}")
                    print(f"[测试输出] 会话控制：消息ID: {getattr(event, 'message_id', 'N/A')}")
                    print(f"[测试输出] 会话控制：时间戳: {getattr(event, 'time', 'N/A')}")
                    print(f"[测试输出] 会话控制：user_input.startswith('/'): {user_input.startswith('/')}")
                    print(f"[测试输出] 会话控制：user_input == '揭晓': {user_input == '揭晓'}")
                    print(f"[测试输出] 会话控制：user_input.startswith('/验证'): {user_input.startswith('/验证')}")
                    print(f"[测试输出] 会话控制：user_input.startswith('揭晓'): {user_input.startswith('揭晓')}")
                    print(f"[测试输出] 会话控制：user_input 的每个字符: {[ord(c) for c in user_input[:10]]}")
                    print(f"[测试输出] 会话控制：user_input 是否以'验证'开头: {user_input.startswith('验证')}")
                    
                    # 特殊处理 /验证 指令
                    if user_input.startswith("/验证"):
                        print(f"[测试输出] 会话控制：检测到 /验证 指令，手动调用验证函数，消息ID: {getattr(event, 'message_id', 'N/A')}")
                        import re
                        match = re.match(r'^/验证\s*(.+)$', user_input)
                        if match:
                            user_guess = match.group(1).strip()
                            print(f"[测试输出] 会话控制：提取验证内容: '{user_guess}'")
                            # 手动调用验证函数
                            await self._handle_verification_in_session(event, user_guess, current_answer)
                            # 检查游戏是否已结束（用户可能猜中了）
                            if not self.game_state.is_game_active(group_id):
                                print(f"[测试输出] 会话控制：游戏已结束，停止会话")
                                controller.stop()
                                return
                        else:
                            await event.send(event.plain_result("请输入要验证的内容，例如：/验证 他是她的父亲"))
                        return
                    elif user_input.startswith("验证"):
                        print(f"[测试输出] 会话控制：检测到 验证 指令（无斜杠），手动调用验证函数")
                        import re
                        match = re.match(r'^验证\s*(.+)$', user_input)
                        if match:
                            user_guess = match.group(1).strip()
                            print(f"[测试输出] 会话控制：提取验证内容: '{user_guess}'")
                            # 手动调用验证函数
                            await self._handle_verification_in_session(event, user_guess, current_answer)
                            # 检查游戏是否已结束（用户可能猜中了）
                            if not self.game_state.is_game_active(group_id):
                                print(f"[测试输出] 会话控制：游戏已结束，停止会话")
                                controller.stop()
                                return
                        else:
                            await event.send(event.plain_result("请输入要验证的内容，例如：验证 他是她的父亲"))
                        return
                    else:
                        print(f"[测试输出] 会话控制：不是 /验证 指令")
                    
                    # 特殊处理 /揭晓 指令
                    if user_input == "揭晓":
                        print(f"[测试输出] 会话控制：检测到 /揭晓 指令，结束会话")
                        # 获取游戏信息并发送答案
                        game = self.game_state.get_game(group_id)
                        if game:
                            answer = game["answer"]
                            puzzle = game["puzzle"]
                            await event.send(event.plain_result(f"🎯 海龟汤游戏结束！\n\n📖 题面：{puzzle}\n📖 完整故事：{answer}\n\n感谢参与游戏！"))
                            self.game_state.end_game(group_id)
                            print(f"[测试输出] 会话控制：已揭晓答案并结束游戏，群ID: {group_id}")
                        controller.stop()
                        return
                    else:
                        print(f"[测试输出] 会话控制：不是 /揭晓 指令")
                    
                    # 检查是否是其他指令，如果是则忽略，让指令处理器处理
                    if user_input.startswith("/"):
                        print(f"[测试输出] 会话控制：检测到指令 '{user_input}'，忽略让指令处理器处理")
                        # 不处理指令，让事件继续传播到指令处理器
                        return
                    else:
                        print(f"[测试输出] 会话控制：不是其他指令")
                    
                    # 移除@bot限制，所有消息都进行游戏问答
                    print(f"[测试输出] 会话控制：处理游戏问答消息: '{user_input}'")
                    
                    # 处理游戏问答消息
                    command_part = user_input.strip()  # 直接使用 plain_text
                    logger.info(f"处理游戏问答消息: '{command_part}'")
                    print(f"[测试输出] 会话控制：处理游戏问答消息: '{command_part}'")
                    
                    # 使用 LLM 判断回答（是否问答）
                    logger.info(f"使用 LLM 判断游戏问答: '{command_part}'")
                    print(f"[测试输出] 会话控制：开始LLM判断")
                    reply = await self.judge_question(command_part, current_answer)
                    print(f"[测试输出] 会话控制：LLM回复: '{reply}'")
                    await event.send(event.plain_result(reply))
                    
                    # 重置超时时间
                    controller.keep(timeout=self.game_timeout, reset_timeout=True)
                    print(f"[测试输出] 会话控制：重置超时时间")
                    
                except Exception as e:
                    logger.error(f"会话控制内部错误: {e}")
                    print(f"[测试输出] 会话控制内部错误: {e}")
                    await event.send(event.plain_result(f"游戏处理过程中发生错误：{e}"))
                    # 如果发生错误，结束游戏
                    self.game_state.end_game(group_id)
                    controller.stop()

            try:
                print(f"[测试输出] 启动游戏会话，群ID: {group_id}")
                await game_session_waiter(event, session_filter=GroupSessionFilter())
            except TimeoutError:
                print(f"[测试输出] 游戏会话超时，群ID: {group_id}")
                game = self.game_state.get_game(group_id)
                if game:
                    await event.send(event.plain_result(f"⏰ 游戏超时！\n\n📖 完整故事：{game['answer']}\n\n游戏结束！"))
                    self.game_state.end_game(group_id)
            except Exception as e:
                logger.error(f"游戏会话错误: {e}")
                print(f"[测试输出] 游戏会话异常: {e}")
                await event.send(event.plain_result(f"游戏过程中发生错误：{e}"))
                self.game_state.end_game(group_id)
        except Exception as e:
            logger.error(f"启动游戏会话失败: {e}")
            print(f"[测试输出] 启动游戏会话失败: {e}")
            await event.send(event.plain_result(f"启动游戏会话失败：{e}"))

    async def _handle_verification_in_session(self, event: AstrMessageEvent, user_guess: str, answer: str):
        """在会话控制中处理验证逻辑"""
        try:
            print(f"[测试输出] 会话验证：开始验证推理: '{user_guess}'")
            
            # 验证用户推理
            result = await self.verify_user_guess(user_guess, answer)
            print(f"[测试输出] 会话验证：验证结果 - 等级:{result.level}, 是否猜中:{result.is_correct}")
            
            # 返回验证结果
            response = f"等级：{result.level}\n评价：{result.comment}"
            await event.send(event.plain_result(response))
            
            # 如果猜中了，结束游戏
            if result.is_correct:
                print(f"[测试输出] 会话验证：用户猜中，结束游戏")
                await event.send(event.plain_result(f"🎉 恭喜！你猜中了！\n\n📖 完整故事：{answer}\n\n游戏结束！"))
                # 结束游戏
                group_id = event.get_group_id()
                if group_id:
                    self.game_state.end_game(group_id)
                # 注意：这里不能直接结束会话，因为会话控制在外层
                # 返回 True 表示需要结束会话，但实际结束由外层处理
                
        except Exception as e:
            logger.error(f"会话验证失败: {e}")
            print(f"[测试输出] 会话验证异常: {e}")
            await event.send(event.plain_result(f"验证过程中发生错误：{e}"))

    # 📊 游戏状态查询
    @filter.command("汤状态")
    async def check_game_status(self, event: AstrMessageEvent):
        """查看当前游戏状态"""
        print(f"[测试输出] /汤状态 指令处理器被调用！")
        print(f"[测试输出] /汤状态 指令：完整消息: '{event.message_str}'")
        group_id = event.get_group_id()
        print(f"[测试输出] 收到 /汤状态 指令，群ID: {group_id}")
        
        if not group_id:
            print("[测试输出] /汤状态 指令：非群聊环境，拒绝执行")
            yield event.plain_result("此功能只能在群聊中使用")
            return

        if self.game_state.is_game_active(group_id):
            game = self.game_state.get_game(group_id)
            print(f"[测试输出] /汤状态 指令：群 {group_id} 有活跃游戏")
            yield event.plain_result(f"🎮 当前有活跃的海龟汤游戏\n📖 题面：{game['puzzle']}")
        else:
            print(f"[测试输出] /汤状态 指令：群 {group_id} 没有活跃游戏")
            yield event.plain_result("🎮 当前没有活跃的海龟汤游戏\n💡 使用 /汤 开始新游戏")

    # 🆘 强制结束游戏（管理员功能）
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("强制结束")
    async def force_end_game(self, event: AstrMessageEvent):
        """强制结束当前游戏（仅管理员）"""
        group_id = event.get_group_id()
        print(f"[测试输出] 收到 /强制结束 指令，群ID: {group_id}")
        
        if not group_id:
            print("[测试输出] /强制结束 指令：非群聊环境，拒绝执行")
            yield event.plain_result("此功能只能在群聊中使用")
            return

        if self.game_state.end_game(group_id):
            print(f"[测试输出] /强制结束 指令：成功结束游戏，群ID: {group_id}")
            yield event.plain_result("✅ 已强制结束当前海龟汤游戏")
        else:
            print(f"[测试输出] /强制结束 指令：没有活跃游戏，群ID: {group_id}")
            yield event.plain_result("❌ 当前没有活跃的游戏需要结束")

    # 📚 备用故事管理指令
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("备用开始")
    async def start_backup_generation(self, event: AstrMessageEvent):
        """开始生成备用故事（仅管理员）"""
        print(f"[测试输出] 收到 /备用开始 指令")
        
        if self.auto_generating:
            print("[测试输出] /备用开始 指令：已在运行中")
            yield event.plain_result("⚠️ 备用故事生成已在运行中")
            return
        
        # 检查存储库是否已满
        storage_info = self.story_storage.get_storage_info()
        if storage_info["available"] <= 0:
            print(f"[测试输出] /备用开始 指令：存储库已满")
            yield event.plain_result("⚠️ 存储库已满，无法生成更多故事")
            return
        
        self.auto_generating = True
        print(f"[测试输出] /备用开始 指令：开始生成，存储库状态: {storage_info['total']}/{storage_info['max_size']}")
        asyncio.create_task(self._auto_generate_loop())
        yield event.plain_result(f"✅ 开始生成备用故事，存储库状态: {storage_info['total']}/{storage_info['max_size']}")

    # 🔒 全局指令拦截器 - 当正在生成时提醒用户
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def global_command_interceptor(self, event: AstrMessageEvent):
        """全局指令拦截器，当正在生成备用故事时提醒用户"""
        # 检查是否有活跃游戏，如果有活跃游戏，不在这里处理
        group_id = event.get_group_id()
        if group_id and self.game_state.is_game_active(group_id):
            # 有活跃游戏，让会话控制处理
            return
        
        # 如果正在生成备用故事，且不是 /备用结束 指令，则提醒用户
        if self.auto_generating:
            user_input = event.message_str.strip()
            # 只拦截非本插件的指令，避免阻断自己的指令
            if (user_input.startswith("/") and 
                not user_input.startswith("/备用结束") and
                not user_input.startswith("/汤") and
                not user_input.startswith("/揭晓") and
                not user_input.startswith("/验证") and
                not user_input.startswith("/汤状态") and
                not user_input.startswith("/强制结束") and
                not user_input.startswith("/备用开始") and
                not user_input.startswith("/备用状态") and
                not user_input.startswith("/汤配置")):
                print(f"[测试输出] 全局拦截器：拦截指令 '{user_input}'")
                yield event.plain_result("⚠️ 系统正在生成备用故事，请稍后再试或使用 /备用结束 停止生成")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("备用结束")
    async def stop_backup_generation(self, event: AstrMessageEvent):
        """停止生成备用故事（仅管理员）"""
        print(f"[测试输出] 收到 /备用结束 指令")
        
        if not self.auto_generating:
            print("[测试输出] /备用结束 指令：未在运行")
            yield event.plain_result("⚠️ 备用故事生成未在运行")
            return
        
        self.auto_generating = False
        print("[测试输出] /备用结束 指令：已停止生成")
        yield event.plain_result("✅ 已停止生成备用故事，正在完成当前生成...")

    @filter.command("备用状态")
    async def check_backup_status(self, event: AstrMessageEvent):
        """查看备用故事状态"""
        print(f"[测试输出] 收到 /备用状态 指令")
        
        storage_info = self.story_storage.get_storage_info()
        status = "🟢 运行中" if self.auto_generating else "🔴 已停止"
        
        print(f"[测试输出] /备用状态 指令：生成状态={status}, 存储库={storage_info['total']}/{storage_info['max_size']}")
        
        message = f"📚 备用故事状态：\n" \
                 f"• 生成状态：{status}\n" \
                 f"• 存储库：{storage_info['total']}/{storage_info['max_size']}\n" \
                 f"• 可用空间：{storage_info['available']}\n" \
                 f"• 自动生成时间：{self.auto_generate_start}:00-{self.auto_generate_end}:00"
        
        yield event.plain_result(message)

    # 🔍 验证指令（仅在非游戏会话时处理）
    @filter.command("验证")
    async def verify_user_guess_command(self, event: AstrMessageEvent, user_guess: str):
        """验证用户推理（仅在非游戏会话时处理）"""
        print(f"[测试输出] /验证 指令处理器被调用！")
        print(f"[测试输出] /验证 指令：完整消息: '{event.message_str}'")
        print(f"[测试输出] /验证 指令：消息ID: {getattr(event, 'message_id', 'N/A')}")
        group_id = event.get_group_id()
        print(f"[测试输出] 收到 /验证 指令，群ID: {group_id}, 推理内容: {user_guess[:30]}...")
        
        if not group_id:
            print("[测试输出] /验证 指令：非群聊环境，拒绝执行")
            yield event.plain_result("验证功能只能在群聊中使用")
            return
        
        # 检查是否有活跃游戏，如果有活跃游戏，说明在会话控制中，不在这里处理
        if self.game_state.is_game_active(group_id):
            print(f"[测试输出] /验证 指令：群 {group_id} 有活跃游戏，由会话控制处理，阻止事件传播")
            # 阻止事件继续传播，避免被会话控制系统重复处理
            await event.block()
            return
        else:
            print(f"[测试输出] /验证 指令：群 {group_id} 没有活跃游戏，独立处理器处理")
        
        # 只有在没有活跃游戏时才在这里处理（用于游戏外的验证）
        print(f"[测试输出] /验证 指令：群 {group_id} 没有活跃游戏，在此处理")
        yield event.plain_result("当前没有活跃的海龟汤游戏，请使用 /汤 开始新游戏")

    # ⚙️ 查看当前配置
    @filter.command("汤配置")
    async def show_config(self, event: AstrMessageEvent):
        """查看当前插件配置"""
        print(f"[测试输出] 收到 /汤配置 指令")
        
        storage_info = self.story_storage.get_storage_info()
        print(f"[测试输出] /汤配置 指令：存储库状态={storage_info['total']}/{storage_info['max_size']}")
        
        config_info = f"⚙️ 海龟汤插件配置：\n" \
                     f"• 生成谜题 LLM：{self.generate_llm_provider_id or '默认'}\n" \
                     f"• 判断问答 LLM：{self.judge_llm_provider_id or '默认'}\n" \
                     f"• 游戏超时：{self.game_timeout} 秒\n" \
                     f"• 存储库大小：{storage_info['total']}/{storage_info['max_size']}\n" \
                     f"• 自动生成时间：{self.auto_generate_start}:00-{self.auto_generate_end}:00"
        yield event.plain_result(config_info)
