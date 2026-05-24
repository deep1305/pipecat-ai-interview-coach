#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""simple-chatbot - Pipecat Voice Agent

This module implements a chatbot using OpenAI for natural language
processing. It includes:
- Real-time audio/video interaction through Daily
- Animated robot avatar
- Text-to-speech using ElevenLabs

The bot runs as part of a pipeline that processes audio/video frames and manages
the conversation flow.

Required AI services:
- Deepgram (Speech-to-Text)
- Openai (LLM)
- ElevenLabs (Text-to-Speech)

Run the bot using::

    uv run bot.py
"""

import os

from dotenv import load_dotenv
from loguru import logger
from PIL import Image
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
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
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIObserver, RTVIProcessor
from pipecat.runner.types import DailyRunnerArguments, RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.groq.llm import GroqLLMService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.daily.transport import DailyParams, DailyTransport
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
import json

load_dotenv(override=True)

sprites = []
script_dir = os.path.dirname(__file__)

# Load sequential animation frames
for i in range(1, 26):
    # Build the full path to the image file
    full_path = os.path.join(script_dir, f"assets/robot0{i}.png")
    # Get the filename without the extension to use as the dictionary key
    # Open the image and convert it to bytes
    with Image.open(full_path) as img:
        sprites.append(OutputImageRawFrame(image=img.tobytes(), size=img.size, format=img.format))

# Create a smooth animation by adding reversed frames
flipped = sprites[::-1]
sprites.extend(flipped)

# Define static and animated states
quiet_frame = sprites[0]  # Static frame for when bot is listening
talking_frame = SpriteFrame(images=sprites)  # Animation sequence for when bot is talking


class TalkingAnimation(FrameProcessor):
    """Manages the bot's visual animation states.

    Switches between static (listening) and animated (talking) states based on
    the bot's current speaking status.
    """

    def __init__(self):
        super().__init__()
        self._is_talking = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process incoming frames and update animation state.

        Args:
            frame: The incoming frame to process
            direction: The direction of frame flow in the pipeline
        """
        await super().process_frame(frame, direction)

        # Switch to talking animation when bot starts speaking
        if isinstance(frame, BotStartedSpeakingFrame):
            if not self._is_talking:
                await self.push_frame(talking_frame)
                self._is_talking = True
        # Return to static frame when bot stops speaking
        elif isinstance(frame, BotStoppedSpeakingFrame):
            await self.push_frame(quiet_frame)
            self._is_talking = False

        await self.push_frame(frame, direction)

def get_config_file_path()-> str:
    script_dir = os.path.dirname(__file__)
    return os.path.join(script_dir,"interview_config.json")


def load_interview_config()-> dict:
    config_file = get_config_file_path()
    default_config = {"botNature":"decent","jd":""}

    try:
        if os.path.exists(config_file):
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
                # Validate and sanitize config
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

async def run_bot(transport: BaseTransport, bot_nature:str = "decent",jd:str=""):
    """Main bot logic."""
    config = load_interview_config()
    bot_nature = config.get("botNature",bot_nature)
    jd = config.get("jd",jd)

    logger.info("Starting bot")
    logger.info(f"Bot nature: {bot_nature}, JD length: {len(jd)} characters")
    # Speech-to-Text service
    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))

    # Text-to-Speech service
    tts = ElevenLabsTTSService(
        api_key=os.getenv("ELEVENLABS_API_KEY"), voice_id="pNInz6obpgDQGcFmaJgB"
    )

    # LLM service
    llm = GroqLLMService(api_key=os.getenv("GROQ_API_KEY"))

    system_prompt = build_system_prompt(bot_nature,jd)
    logger.info(system_prompt)
    

    messages = [
        {
            "role": "system",
            "content": system_prompt
        },
    ]

    # Set up conversation context and management
    # The context_aggregator will automatically collect conversation context
    context = LLMContext(messages)
    context_aggregator = LLMContextAggregatorPair(context)

    rtvi = RTVIProcessor()

    ta = TalkingAnimation()

    # Pipeline - assembled from reusable components
    pipeline = Pipeline(
        [
            transport.input(),
            rtvi,
            stt,
            context_aggregator.user(),
            llm,
            tts,
            ta,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        observers=[
            RTVIObserver(rtvi),
        ],
    )

    # Queue initial static frame so video starts immediately
    await task.queue_frame(quiet_frame)

    @rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        await rtvi.set_bot_ready()
        # Kick off the conversation
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
    """Main bot entry point."""

    transport = None

    match runner_args:
        case DailyRunnerArguments():
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
                    vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.2)),
                    turn_analyzer=LocalSmartTurnAnalyzerV3(),
                ),
            )
        case SmallWebRTCRunnerArguments():
            webrtc_connection: SmallWebRTCConnection = runner_args.webrtc_connection

            transport = SmallWebRTCTransport(
                webrtc_connection=webrtc_connection,
                params=TransportParams(
                    audio_in_enabled=True,
                    audio_out_enabled=True,
                    video_out_enabled=True,
                    video_out_width=1024,
                    video_out_height=576,
                    vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.2)),
                    turn_analyzer=LocalSmartTurnAnalyzerV3(),
                ),
            )
        case _:
            logger.error(f"Unsupported runner arguments type: {type(runner_args)}")
            return

    await run_bot(transport)


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
