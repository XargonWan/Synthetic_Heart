FAQ
===

.. image:: res/faq.png
    :width: 600px
    :alt: FAQ illustration


What does "Synthetic Heart" mean? Is it emulating a biological heart?
-------------------------------------------------------------------

Not in the biological sense. "Heart" refers to the center of emotions, feelings, and soul in many cultures—essentially, giving the synth a "soul" or emotional core.

Isn't the LLM the core of the AI? Changing it would make it a different persona.
-------------------------------------------------------------------------------

From testing, swapping LLMs (e.g., ChatGPT to Gemini) didn't drastically change the persona. However, different LLMs can subtly alter personality traits or opinions. Self-hosted LLMs are preferable but currently unaffordable by me, the project founder, so I never created a local LLM Cortex.

Does the LLM lie or hallucinate?
--------------------------------

SyntH instructs LLMs not to lie and now explicitly prefers honest uncertainty over fabricated recollection when memory is weak or unclear. This improves trust, but some models like certain versions of ChatGPT may still produce inaccurate information—don't expect miracles.

Can I integrate the synth into video games to play together?
-----------------------------------------------------------

Theoretically yes, depending on the game, anti-cheat systems, and interface development. If other AIs can play games like Minecraft, SyntH should be able to as well, though this hasn't been fully explored.
I believe the Live Cortex might be a good use case for game integration, but it requires a Gemini API key and is still experimental.

Can the synth speak, not just text?
-----------------------------------

Yes, the Gemini Live Cortex supports real-time voice interactions, allowing the synth to speak and listen in supported platforms like Discord. This feature is still experimental and requires a valid Gemini API key.
Moreover a text-to-speech plugin using local TTS engines could be developed to enable speech without relying on external APIs.

If the synth communicates on multiple platforms like Telegram and Discord, is it the same persona?
-----------------------------------------------------------------------------------------------

Yes, it's the same entity. Think of it as a person using different apps on their phone—you can start a conversation on Telegram and continue on Discord.

What about privacy concerns?
----------------------------

Privacy is a complex issue not fully addressed yet. General advice: avoid sharing private information with AIs unless self-hosted and not in public groups. You can request privacy, and synths usually comply, but it's not guaranteed.
SyntHs are not collecting user data except for the Trainer ones. This data is stored in the database and can be deleted by the user at any time.

Why doesn't the synth have more animations in 3D space to seem more alive?
--------------------------------------------------------------------------

Animations should reflect actual activities, not fake ones for realism. The goal is true realism, not simulation, so you won't see the synth eating or sleeping as it something that a SyntH doesn't actually do. The Web UI focuses on facial expressions and lip-syncing to reflect the synth's "emotional state" and conversational engagement.
We wish to expand this concept in the future with more dynamic expressions and interactions, but it will always be based on real activities and states of the synth rather than arbitrary animations.
