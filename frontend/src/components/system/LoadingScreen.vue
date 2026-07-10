<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  /** 0–1, or null while the phase has no measurable size (e.g. animation
   * retargeting) — shown as an indeterminate pulse. */
  progress: number | null
  label: string
}>()

const percent = computed(() => (props.progress == null ? null : Math.round(props.progress * 100)))
</script>

<template>
  <Transition name="fade">
    <div class="absolute inset-0 z-30 flex flex-col items-center justify-center gap-3 bg-primary-950/90 text-primary-100 backdrop-blur-sm">
      <div class="text-lg font-medium tracking-wide">
        {{ label }}
      </div>
      <div class="h-1 w-48 overflow-hidden rounded-full bg-white/10">
        <div
          class="h-full rounded-full bg-primary-400 transition-[width]"
          :class="percent == null ? 'animate-pulse w-full' : ''"
          :style="percent == null ? undefined : { width: `${percent}%` }"
        />
      </div>
      <div v-if="percent != null" class="text-xs text-primary-300/80">
        {{ percent }}%
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.3s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
