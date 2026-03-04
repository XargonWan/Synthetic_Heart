"""Example: Using KaradaStateServer from a Plugin

This example demonstrates how to integrate the animation state server
into a custom plugin or component.
"""

import asyncio
from core.animation_handler import get_karada_state_server, AnimationState
from core.plugin_base import PluginBase


class ExampleAnimationPlugin(PluginBase):
    """Example plugin that triggers animations based on processing stages."""

    def __init__(self):
        super().__init__()
        self.animation_handler = get_karada_state_server()
        self.current_session = None

    async def process_with_animations(self, message, session_id):
        """Process a message with animation feedback.

        This demonstrates the typical flow:
        1. Start "think" animation when processing begins
        2. Transition to "write" when generating output
        3. Return to "idle" when complete
        """
        # Generate unique context ID for this processing task
        context_id = f"plugin_task_{message.message_id}"

        try:
            # Stage 1: Thinking/Processing
            await self.animation_handler.transition_to(
                AnimationState.THINK, session_id=session_id, context_id=context_id
            )

            # Simulate processing time
            await asyncio.sleep(1.0)
            analysis_result = await self.analyze_message(message)

            # Stage 2: Writing/Generating Response
            await self.animation_handler.transition_to(
                AnimationState.WRITE, session_id=session_id, context_id=context_id
            )

            # Simulate response generation
            await asyncio.sleep(2.0)
            response = await self.generate_response(analysis_result)

            return response

        finally:
            # Always clean up animation context
            await self.animation_handler.stop_animation(context_id, session_id)
            # This will return to Idle if no other contexts are active

    async def analyze_message(self, message):
        """Analyze the incoming message."""
        # Your analysis logic here
        return {"intent": "question", "sentiment": "neutral"}

    async def generate_response(self, analysis):
        """Generate a response based on analysis."""
        # Your generation logic here
        return "This is a generated response."


class BackgroundTaskPlugin(PluginBase):
    """Example plugin that runs background tasks with animations."""

    def __init__(self):
        super().__init__()
        self.animation_handler = get_karada_state_server()
        self.background_tasks = {}

    async def start_background_task(self, task_id, session_id):
        """Start a long-running background task with animation.

        This demonstrates how to manage animations for tasks that
        run independently of message processing.
        """
        context_id = f"bg_task_{task_id}"

        # Start animation
        await self.animation_handler.play_animation(
            AnimationState.WRITE,
            session_id=session_id,
            loop=True,
            context_id=context_id,
        )

        # Store task info
        self.background_tasks[task_id] = {
            "session_id": session_id,
            "context_id": context_id,
            "task": asyncio.create_task(self._run_task(task_id)),
        }

    async def stop_background_task(self, task_id):
        """Stop a background task and its animation."""
        if task_id not in self.background_tasks:
            return

        task_info = self.background_tasks[task_id]

        # Cancel the task
        task_info["task"].cancel()

        # Stop the animation
        await self.animation_handler.stop_animation(
            task_info["context_id"], task_info["session_id"]
        )

        del self.background_tasks[task_id]

    async def _run_task(self, task_id):
        """Internal task implementation."""
        try:
            while True:
                await asyncio.sleep(1.0)
                # Do work...
        except asyncio.CancelledError:
            pass


class ConditionalAnimationPlugin(PluginBase):
    """Example plugin that conditionally triggers animations."""

    def __init__(self):
        super().__init__()
        self.animation_handler = get_karada_state_server()

    async def process_with_conditional_animation(self, message, session_id):
        """Only trigger animations for certain message types."""
        # Check if this is a WebUI session
        # (animations only work for WebUI)
        if not self._is_webui_session(session_id):
            # Process without animations
            return await self.process_without_animation(message)

        # Check message complexity
        complexity = self._assess_complexity(message)

        if complexity == "simple":
            # Simple messages don't need "think" animation
            context_id = f"simple_{message.message_id}"

            await self.animation_handler.transition_to(
                AnimationState.WRITE, session_id=session_id, context_id=context_id
            )

            try:
                response = await self.quick_response(message)
                return response
            finally:
                await self.animation_handler.stop_animation(context_id, session_id)

        else:
            # Complex messages get full animation sequence
            context_id = f"complex_{message.message_id}"

            try:
                await self.animation_handler.transition_to(
                    AnimationState.THINK, session_id=session_id, context_id=context_id
                )

                await asyncio.sleep(0.5)

                await self.animation_handler.transition_to(
                    AnimationState.WRITE, session_id=session_id, context_id=context_id
                )

                response = await self.detailed_response(message)
                return response
            finally:
                await self.animation_handler.stop_animation(context_id, session_id)

    def _is_webui_session(self, session_id):
        """Check if session is a WebUI session."""
        # WebUI sessions typically have UUID format
        return len(session_id) == 36 and "-" in session_id

    def _assess_complexity(self, message):
        """Assess message complexity."""
        word_count = len(message.text.split())
        if word_count < 10:
            return "simple"
        else:
            return "complex"

    async def quick_response(self, message):
        """Generate a quick response."""
        await asyncio.sleep(0.5)
        return "Quick response"

    async def detailed_response(self, message):
        """Generate a detailed response."""
        await asyncio.sleep(2.0)
        return "Detailed response"

    async def process_without_animation(self, message):
        """Process message without triggering animations."""
        # Standard processing for non-WebUI interfaces
        return "Response without animation"


# ============================================================================
# Usage Examples
# ============================================================================


async def example_basic_usage():
    """Basic animation usage."""
    handler = get_karada_state_server()
    session_id = "example-session-123"

    # Start thinking
    await handler.transition_to(
        AnimationState.THINK, session_id=session_id, context_id="example_1"
    )

    await asyncio.sleep(1.0)

    # Switch to writing
    await handler.transition_to(
        AnimationState.WRITE, session_id=session_id, context_id="example_1"
    )

    await asyncio.sleep(2.0)

    # Return to idle
    await handler.stop_animation("example_1", session_id)


async def example_multiple_contexts():
    """Managing multiple animation contexts."""
    handler = get_karada_state_server()
    session_id = "example-session-456"

    # Start first task
    await handler.play_animation(
        AnimationState.WRITE, session_id=session_id, context_id="task_1"
    )

    # Start second task (will override animation)
    await handler.play_animation(
        AnimationState.THINK, session_id=session_id, context_id="task_2"
    )

    # Complete first task (doesn't stop animation - task_2 still active)
    await handler.stop_animation("task_1", session_id)

    await asyncio.sleep(1.0)

    # Complete second task (returns to Idle)
    await handler.stop_animation("task_2", session_id)


async def example_error_handling():
    """Animation with error handling."""
    handler = get_karada_state_server()
    session_id = "example-session-789"
    context_id = "error_example"

    try:
        await handler.transition_to(
            AnimationState.THINK, session_id=session_id, context_id=context_id
        )

        # Simulate processing that might fail
        await some_risky_operation()

        await handler.transition_to(
            AnimationState.WRITE, session_id=session_id, context_id=context_id
        )

    except Exception as e:
        print(f"Error occurred: {e}")
        # Animation will still be cleaned up in finally block
    finally:
        # Always clean up animation, even on error
        await handler.stop_animation(context_id, session_id)


async def some_risky_operation():
    """Placeholder for operation that might fail."""
    await asyncio.sleep(0.5)


if __name__ == "__main__":
    # Run examples
    asyncio.run(example_basic_usage())
    asyncio.run(example_multiple_contexts())
    asyncio.run(example_error_handling())
