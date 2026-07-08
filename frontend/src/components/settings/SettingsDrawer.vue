<script setup lang="ts">
import SkinSelector from './SkinSelector.vue'
import { useSettingsStore } from '@/stores/settings'
import { CAMERA_PRESETS } from '@/composables/vrm/scene'

defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: [] }>()

const settings = useSettingsStore()
const presets = Object.keys(CAMERA_PRESETS) as Array<keyof typeof CAMERA_PRESETS>
</script>

<template>
  <Transition name="drawer">
    <aside
      v-if="open"
      class="absolute inset-y-0 right-0 z-20 w-72 flex flex-col gap-5 overflow-y-auto bg-black/55 p-4 text-white backdrop-blur-lg"
    >
      <div class="flex items-center justify-between">
        <h2 class="text-sm font-semibold tracking-wide">
          Settings
        </h2>
        <button class="text-white/60 hover:text-white" @click="emit('close')">
          ✕
        </button>
      </div>

      <section class="flex flex-col gap-2">
        <h3 class="text-xs uppercase tracking-wide text-white/50">
          Skin
        </h3>
        <SkinSelector />
      </section>

      <section class="flex flex-col gap-2">
        <h3 class="text-xs uppercase tracking-wide text-white/50">
          Camera
        </h3>
        <div class="flex gap-2">
          <button
            v-for="preset in presets"
            :key="preset"
            class="flex-1 rounded-lg px-2 py-1.5 text-xs capitalize transition"
            :class="settings.cameraPreset === preset
              ? 'bg-primary-500/80 text-white'
              : 'bg-white/10 text-white/70 hover:bg-white/20'"
            @click="settings.cameraPreset = preset"
          >
            {{ preset.replace('-', ' ') }}
          </button>
        </div>
      </section>

      <section class="flex flex-col gap-2">
        <h3 class="text-xs uppercase tracking-wide text-white/50">
          Theme hue
        </h3>
        <input
          v-model.number="settings.themeHue"
          type="range"
          min="0"
          max="360"
          step="1"
          class="w-full accent-primary-400"
        >
      </section>

      <p class="mt-auto text-[11px] leading-relaxed text-white/40">
        SyntH Stage — connects to this server's <code>/ws</code> and
        <code>/api/karada</code> endpoints. Portions ported from
        <a href="https://github.com/moeru-ai/airi" target="_blank" rel="noopener" class="underline">
          Project AIRI
        </a> (MIT).
      </p>
    </aside>
  </Transition>
</template>

<style scoped>
.drawer-enter-active,
.drawer-leave-active {
  transition: transform 0.25s ease;
}
.drawer-enter-from,
.drawer-leave-to {
  transform: translateX(100%);
}
</style>
