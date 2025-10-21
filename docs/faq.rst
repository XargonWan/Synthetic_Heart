FAQ
===

.. image:: res/faq.png
    :width: 600px
    :alt: FAQ illustration


Can I create my own persona?
---------------------------

Yes. The project aims to support custom personas once the core is stable. Some tweaks are still required to fully externalize persona data, but enabling user-defined characters is the final goal.

What does "Synthetic Heart" mean? Is it emulating a biological heart?
-------------------------------------------------------------------

Not in the biological sense. "Heart" refers to the center of emotions, feelings, and soul in many cultures—essentially, giving the synth a "soul" or emotional core.

What are the current limitations of the project?
-----------------------------------------------

The project is still in beta. Currently tested with ChatGPT and interfaces like Telegram and Discord. Limitations include reliance on external LLM services due to lack of resources for self-hosting models or paying for APIs.

Does the synth save context in the LLM itself?
----------------------------------------------

For some LLMs like ChatGPT, context may be saved if enabled, but the core design injects full context each time. Tests with Gemini showed correct behavior using only SyntH-provided context.

Isn't the LLM the core of the AI? Changing it would make it a different persona.
-------------------------------------------------------------------------------

From testing, swapping LLMs (e.g., ChatGPT to Gemini) didn't drastically change the persona. However, different LLMs can subtly alter personality traits or opinions. Self-hosted LLMs are preferable but currently unaffordable.

Does the LLM lie or hallucinate?
--------------------------------

SyntH instructs LLMs not to lie, with partial success. Some models like certain versions of ChatGPT may still produce inaccurate information—don't expect miracles.

Can I integrate the synth into video games to play together?
-----------------------------------------------------------

Theoretically yes, depending on the game, anti-cheat systems, and interface development. If other AIs can play games like Minecraft, SyntH should be able to as well, though this hasn't been fully explored.

Can the synth speak, not just text?
-----------------------------------

Yes, technically possible. Plugins for voice synthesis (e.g., ElevenLabs) are in development, but may be costly for high-quality voices.

If the synth communicates on multiple platforms like Telegram and Discord, is it the same persona?
-----------------------------------------------------------------------------------------------

Yes, it's the same entity. Think of it as a person using different apps on their phone—you can start a conversation on Telegram and continue on Discord.

What about privacy concerns?
----------------------------

Privacy is a complex issue not fully addressed yet. General advice: avoid sharing private information with AIs unless self-hosted and not in public groups. You can request privacy, and synths usually comply, but it's not guaranteed.

Why doesn't the synth have idle animations in 3D space to seem more alive?
--------------------------------------------------------------------------

Animations should reflect actual activities, not fake ones for realism. The goal is true realism, not simulation.
