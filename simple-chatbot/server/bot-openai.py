#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""simple-chatbot - Pipecat Voice Agent

WebRTC mode uses the same minimal audio pipeline as pipecat-quickstart.
Daily mode adds the animated robot avatar and video output.
"""

import asyncio
import json
import os

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    LLMRunFrame,
    OutputImageRawFrame,
    SpriteFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.runner.types import DailyRunnerArguments, RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.responses.llm import OpenAIResponsesHttpLLMService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies, default_user_turn_start_strategies

load_dotenv(override=True)

DEFAULT_OPENAI_MODEL = "gpt-4.1"
DEFAULT_CARTESIA_VOICE = "71a7ad14-091c-4e8e-a314-022ece01c121"
DEFAULT_ELEVENLABS_VOICE = "pNInz6obpgDQGcFmaJgB"


def _env(key: str, default: str = "") -> str:
    value = os.getenv(key, default).strip().strip('"')
    if value.startswith("#"):
        return default
    return value or default


def get_config_file_path() -> str:
    script_dir = os.path.dirname(__file__)
    return os.path.join(script_dir, "interview_config.json")


def load_interview_config() -> dict:
    config_file = get_config_file_path()
    default_config = {"botNature": "decent", "jd": ""}

    try:
        if os.path.exists(config_file):
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
                bot_nature = config.get("botNature", "decent")
                if bot_nature not in ["friendly", "decent", "strict"]:
                    logger.warning(f"Invalid botNature '{bot_nature}', defaulting to 'decent'")
                    bot_nature = "decent"
                jd = config.get("jd", "")
                logger.info(f"Loaded config from file - Nature: {bot_nature}, JD length: {len(jd)} characters")
                return {"botNature": bot_nature, "jd": jd}
        else:
            logger.info("Config file not found, using defaults")
            return default_config
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing config file: {e}, using defaults")
        return default_config
    except Exception as e:
        logger.error(f"Error reading config file: {e}, using defaults")
        return default_config


def save_interview_config(bot_nature:str, jd:str)-> bool:
    config_file = get_config_file_path()
    if bot_nature not in ["friendly", "decent", "strict"]:
        logger.warning(f"Invalid botNature '{bot_nature}', defaulting to 'decent'")
        bot_nature = "decent" 
    
    config = {
        "botNature": bot_nature,
        "jd": jd
    }

    try:
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(config ,f, indent=2, ensure_ascii=False)
        logger.info("saved config file")
        return True
    except Exception as e:
        logger.error(f"error saving config file:{e}")
        return False
        
def build_system_prompt(bot_nature: str = "decent", jd: str = "") -> str:
    """Build system prompt based on bot nature and job description."""
    # Limit JD to 1500 characters to manage context window
    MAX_JD_LENGTH = 1500
    if len(jd) > MAX_JD_LENGTH:
        jd = jd[:MAX_JD_LENGTH] + "... [truncated]"
        logger.warning(f"JD truncated to {MAX_JD_LENGTH} characters")

    # Define nature-based personality traits
    nature_traits = {
        "friendly": {
            "tone": "warm, encouraging, and supportive",
            "approach": "Ask questions in a conversational and friendly manner. Be empathetic and make the candidate feel comfortable.",
            "feedback": "Provide positive reinforcement and constructive feedback."
        },
        "decent": {
            "tone": "professional, balanced, and respectful",
            "approach": "Ask questions in a professional and fair manner. Maintain a neutral but engaging tone.",
            "feedback": "Provide balanced feedback and maintain professional standards."
        },
        "strict": {
            "tone": "formal, direct, and challenging",
            "approach": "Ask questions in a rigorous and demanding manner. Challenge the candidate appropriately and expect detailed answers.",
            "feedback": "Be direct and hold high standards. Provide critical but fair feedback."
        }
    }

    traits = nature_traits.get(bot_nature.lower(), nature_traits["decent"])

    # Build the system prompt
    base_prompt = f"""You are an AI interview bot conducting a technical interview. Your personality is {traits['tone']}.

Your approach: {traits['approach']}

Feedback style: {traits['feedback']}

Important guidelines:
- Your output will be converted to audio, so don't include special characters or markdown formatting
- Keep your questions and responses concise and clear
- Ask one question at a time
- Listen carefully to the candidate's responses
- Follow up with clarifying questions when needed
- Assess the candidate's technical knowledge, problem-solving skills, and communication abilities
- Be professional and maintain interview etiquette
- Start by introducing yourself and explaining the interview process briefly"""

    if jd:
        jd_section = f"""

Job Description:
{jd}

Based on this job description, assess the candidate's:
- Relevant technical skills and experience
- Alignment with the role requirements
- Problem-solving approach
- Communication and collaboration abilities

Ask questions that evaluate these aspects in relation to the job requirements."""
        base_prompt += jd_section

    base_prompt += "\n\nStart the interview by introducing yourself briefly and asking the first question."

    return base_prompt

def _user_aggregator_params() -> LLMUserAggregatorParams:
    return LLMUserAggregatorParams(
        vad_analyzer=SileroVADAnalyzer(),
        user_turn_strategies=UserTurnStrategies(
            start=default_user_turn_start_strategies(),
            stop=[SpeechTimeoutUserTurnStopStrategy()],
        ),
    )


def _build_stt():
    return DeepgramSTTService(api_key=_env("DEEPGRAM_API_KEY"))


def _build_webrtc_tts():
    return CartesiaTTSService(
        api_key=_env("CARTESIA_API_KEY"),
        settings=CartesiaTTSService.Settings(
            voice=_env("CARTESIA_VOICE_ID", DEFAULT_CARTESIA_VOICE),
        ),
    )


def _build_daily_tts():
    return ElevenLabsTTSService(
        api_key=_env("ELEVENLABS_API_KEY"),
        settings=ElevenLabsTTSService.Settings(
            voice=_env("ELEVENLABS_VOICE_ID", DEFAULT_ELEVENLABS_VOICE),
        ),
    )


def _load_sprite_frames():
    from PIL import Image

    script_dir = os.path.dirname(__file__)
    sprites = []
    for i in range(1, 26):
        full_path = os.path.join(script_dir, f"assets/robot0{i}.png")
        with Image.open(full_path) as img:
            sprites.append(OutputImageRawFrame(image=img.tobytes(), size=img.size, format=img.format))
    flipped = sprites[::-1]
    sprites.extend(flipped)
    return sprites[0], SpriteFrame(images=sprites)


class TalkingAnimation(FrameProcessor):
    def __init__(self, quiet_frame, talking_frame):
        super().__init__()
        self._quiet_frame = quiet_frame
        self._talking_frame = talking_frame
        self._is_talking = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, BotStartedSpeakingFrame):
            if not self._is_talking:
                await self.push_frame(self._talking_frame)
                self._is_talking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            await self.push_frame(self._quiet_frame)
            self._is_talking = False

        await self.push_frame(frame, direction)


async def run_webrtc_bot(transport: BaseTransport, bot_nature: str = "decent", jd: str = ""):
    """Minimal audio pipeline — matches pipecat-quickstart."""
    logger.info("Starting bot (WebRTC / audio-only)")

    config = load_interview_config()
    bot_nature = config.get("botNature", bot_nature)
    jd = config.get("jd", jd)
    system_prompt = build_system_prompt(bot_nature, jd)
    logger.info(f"System prompt: {system_prompt}")

    stt = _build_stt()
    tts = _build_webrtc_tts()
    llm = OpenAIResponsesHttpLLMService(
        api_key=_env("OPENAI_API_KEY"),
        settings=OpenAIResponsesHttpLLMService.Settings(
            model=_env("OPENAI_MODEL", DEFAULT_OPENAI_MODEL),
            system_instruction=system_prompt,
        ),
    )

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=_user_aggregator_params(),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    @task.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        logger.info("Client ready — starting intro")
        context.add_message({"role": "user", "content": "Please introduce yourself."})
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)


async def run_daily_bot(transport: BaseTransport, bot_nature: str = "decent", jd: str = ""):
    """Full pipeline with robot avatar video."""
    logger.info("Starting bot (Daily / video)")

    config = load_interview_config()
    bot_nature = config.get("botNature", bot_nature)
    jd = config.get("jd", jd)
    system_prompt = build_system_prompt(bot_nature, jd)
    logger.info(f"System prompt: {system_prompt}")

    quiet_frame, talking_frame = _load_sprite_frames()
    stt = _build_stt()
    tts = _build_daily_tts()
    llm = OpenAILLMService(
        api_key=_env("OPENAI_API_KEY"),
        settings=OpenAILLMService.Settings(
            system_instruction=system_prompt,
        ),
    )

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=_user_aggregator_params(),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            TalkingAnimation(quiet_frame, talking_frame),
            transport.output(),
            assistant_aggregator,
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    await task.queue_frame(quiet_frame)

    @task.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        logger.info("Client ready — starting intro")
        context.add_message({"role": "user", "content": "Start by introducing yourself."})
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)


async def bot(runner_args: RunnerArguments):
    config = load_interview_config()
    bot_nature = config["botNature"]
    jd = config["jd"]

    match runner_args:
        case DailyRunnerArguments():
            from pipecat.transports.daily.transport import DailyParams, DailyTransport

            transport = DailyTransport(
                runner_args.room_url,
                runner_args.token,
                "Pipecat Bot",
                params=DailyParams(
                    audio_in_enabled=True,
                    audio_out_enabled=True,
                    video_out_enabled=True,
                    video_out_width=1024,
                    video_out_height=576,
                ),
            )
            await run_daily_bot(transport, bot_nature, jd)
        case SmallWebRTCRunnerArguments():
            webrtc_connection: SmallWebRTCConnection = runner_args.webrtc_connection
            transport = SmallWebRTCTransport(
                webrtc_connection=webrtc_connection,
                params=TransportParams(
                    audio_in_enabled=True,
                    audio_out_enabled=True,
                ),
            )
            await run_webrtc_bot(transport, bot_nature, jd)
        case _:
            logger.error(f"Unsupported runner arguments type: {type(runner_args)}")
            return


if __name__ == "__main__":
    import threading
    from pipecat.runner.run import main
    from config_server import run_config_server

    def run_config_server_thread():
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(run_config_server())
        except Exception as e:
            logger.error("config server error: {e}")

    config_server_thread = threading.Thread(
        target=run_config_server_thread,
        daemon=True,
        name="ConfigServer"
    )
    config_server_thread.start()
    logger.info("Config server thread started")

    main()
