<script setup lang="ts">
import type { SkinInfo } from '@/services/karada-rest'

import { onMounted, ref } from 'vue'

import { activateSkin, fetchSkins } from '@/services/karada-rest'
import { useAvatarStore } from '@/stores/avatar'

const skins = ref<SkinInfo[]>([])
const switching = ref<string | null>(null)
const avatar = useAvatarStore()

onMounted(async () => {
  try {
    skins.value = await fetchSkins()
  }
  catch {
    skins.value = []
  }
})

function activeName(): string | null {
  const url = avatar.model?.url
  if (!url)
    return null
  return /\/skins\/([^/]+)\//.exec(url)?.[1] ?? null
}

async function selectSkin(name: string): Promise<void> {
  if (name === activeName() || switching.value)
    return
  switching.value = name
  try {
    // Server stays the source of truth: it broadcasts `vrm_model` (and plays
    // the skin_change animation) to every connected client, this one included.
    await activateSkin(name)
  }
  catch {
    // best-effort — the button simply stays clickable again
  }
  finally {
    switching.value = null
  }
}
</script>

<template>
  <div class="grid grid-cols-3 gap-2">
    <button
      v-for="skin in skins"
      :key="skin.name"
      class="group relative aspect-square overflow-hidden rounded-lg ring-2 transition"
      :class="skin.name === activeName() ? 'ring-primary-400' : 'ring-white/10 hover:ring-white/30'"
      :disabled="!!switching"
      @click="selectSkin(skin.name)"
    >
      <div class="absolute inset-0 flex items-center justify-center bg-primary-900/50 text-xs text-primary-200">
        {{ skin.name }}
      </div>
      <img
        :src="`/skins/${skin.name}/preview.png`"
        :alt="skin.name"
        class="absolute inset-0 h-full w-full object-cover"
        @error="($event.target as HTMLImageElement).style.visibility = 'hidden'"
      >
      <div class="absolute inset-0 ring-1 ring-inset ring-white/5" />
      <div class="absolute inset-x-0 bottom-0 truncate bg-black/50 px-1.5 py-0.5 text-[11px] text-white">
        {{ skin.name }}
      </div>
      <div
        v-if="switching === skin.name"
        class="absolute inset-0 flex items-center justify-center bg-black/50 text-xs text-white"
      >
        …
      </div>
    </button>
  </div>
</template>
