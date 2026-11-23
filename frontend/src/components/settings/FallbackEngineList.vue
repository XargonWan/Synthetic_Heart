<script setup lang="ts">
import type { FallbackRoute } from '@/services/voice-config'

import { onMounted, ref } from 'vue'

import { getFallbackEngines, setFallbackEngines } from '@/services/voice-config'

// Ordered list of fallback engines for one route. The backend tries them in
// order when the active engine is unavailable; the list is persisted to
// <ROUTE>_FALLBACK_ENGINES.
const props = defineProps<{
  route: FallbackRoute
  /** Engines that can be picked (names), excluding disabled/inline pseudo-engines. */
  engines: Array<{ name: string, label: string }>
  /** Currently active engine; not offered when adding a fallback. */
  active: string | null
  disabled?: boolean
}>()

const fallbacks = ref<string[]>([])
const saving = ref(false)

onMounted(async () => {
  try {
    fallbacks.value = await getFallbackEngines(props.route)
  }
  catch {
    fallbacks.value = []
  }
})

async function persist(): Promise<void> {
  saving.value = true
  try {
    await setFallbackEngines(props.route, fallbacks.value)
  }
  finally {
    saving.value = false
  }
}

async function add(): Promise<void> {
  const next = props.engines.find(e => e.name !== props.active && !fallbacks.value.includes(e.name))
  if (!next)
    return
  fallbacks.value = [...fallbacks.value, next.name]
  await persist()
}

async function change(index: number, name: string): Promise<void> {
  fallbacks.value = fallbacks.value.map((n, i) => (i === index ? name : n))
  await persist()
}

async function move(index: number, delta: number): Promise<void> {
  const to = index + delta
  const list = [...fallbacks.value]
  const a = list[index]
  const b = list[to]
  if (a === undefined || b === undefined)
    return
  list[index] = b
  list[to] = a
  fallbacks.value = list
  await persist()
}

async function remove(index: number): Promise<void> {
  fallbacks.value = fallbacks.value.filter((_, i) => i !== index)
  await persist()
}
</script>

<template>
  <div class="mt-3 flex flex-col gap-1.5 border-t border-white/10 pt-3">
    <div class="flex items-center justify-between">
      <span class="text-[11px] text-white/40">Fallback</span>
      <button
        class="rounded-lg bg-white/10 px-2 py-1 text-[11px] text-white/70 transition hover:bg-white/20"
        :disabled="disabled || saving"
        title="Add a fallback engine"
        @click="add"
      >
        ＋
      </button>
    </div>
    <p class="text-[10px] text-white/30">
      Engines tried in order when the active one is unavailable.
    </p>
    <div v-for="(name, index) in fallbacks" :key="`${index}-${name}`" class="flex items-center gap-1.5">
      <span class="w-4 text-[11px] text-white/40">{{ index + 1 }}.</span>
      <select
        class="rounded-lg bg-white/10 px-2 py-1 text-xs text-white/80"
        :value="name"
        :disabled="disabled || saving"
        @change="change(index, ($event.target as HTMLSelectElement).value)"
      >
        <option v-for="e in engines" :key="e.name" :value="e.name">
          {{ e.label }}
        </option>
        <option v-if="!engines.some(e => e.name === name)" :value="name">
          {{ name }} (unavailable)
        </option>
      </select>
      <button
        class="rounded-lg bg-white/10 px-2 py-1 text-[11px] text-white/70 hover:bg-white/20"
        :disabled="disabled || saving || index === 0"
        title="Try earlier"
        @click="move(index, -1)"
      >
        ↑
      </button>
      <button
        class="rounded-lg bg-white/10 px-2 py-1 text-[11px] text-white/70 hover:bg-white/20"
        :disabled="disabled || saving || index === fallbacks.length - 1"
        title="Try later"
        @click="move(index, 1)"
      >
        ↓
      </button>
      <button
        class="rounded-lg bg-white/10 px-2 py-1 text-[11px] text-white/70 hover:bg-white/20"
        :disabled="disabled || saving"
        title="Remove"
        @click="remove(index)"
      >
        ✕
      </button>
    </div>
  </div>
</template>
