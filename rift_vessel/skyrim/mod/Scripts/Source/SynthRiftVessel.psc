Scriptname SynthRiftVessel extends Quest  
;
; Synthetic Heart — Skyrim Rift Vessel
; =====================================
; Attach this quest to an Actor (the NPC) and call Start() to begin
; the IPC polling loop.  The NPC is then controllable by SyntH.
;
; Installation:
;  1. Place this script on a Quest in the Creation Kit.
;  2. Set the NPC reference alias as SynthNPCAlias.
;  3. Start the quest via a button or OnPlayerLoadGame.
;  4. Set the papyrus property SynthNPC to the actor ref.
;
; The SKSE plugin (synth_rift_vessel.dll) provides:
;   UpdateWorldState()  — pushes NPC state to the IPC pipe
;   ExecuteAction()     — dispatches actions from SyntH

Actor Property SynthNPC Auto
Float Property PollInterval = 1.0 Auto   ; seconds between state updates

; ─── Quest events ─────────────────────────────────────────────────────────────

Event OnQuestInit()
    ; Register for the update loop
    RegisterForSingleUpdate(PollInterval)
    Debug.Trace("[SynthRiftVessel] Quest initialised, NPC=" + SynthNPC)
EndEvent

Event OnUpdate()
    If SynthNPC == None || SynthNPC.IsDeleted()
        UnregisterForUpdate()
        Return
    EndIf

    ; 1. Tell the SKSE plugin to push world state to SyntH
    UpdateWorldState()

    ; 2. Poll for pending actions from SyntH (stored in a global by the SKSE plugin)
    String action = JDB.solveStr(".SynthRiftVessel.pending_action")
    If action != ""
        String payload = JDB.solveStr(".SynthRiftVessel.pending_payload")
        Debug.Trace("[SynthRiftVessel] Received action: " + action)
        
        ; Execute the action on the NPC
        ExecuteActionOnNPC(action, payload)
        
        ; Clear the pending action
        JDB.solveSetter(".SynthRiftVessel.pending_action", "")
        JDB.solveSetter(".SynthRiftVessel.pending_payload", "")
    EndIf

    ; 3. Re-register
    RegisterForSingleUpdate(PollInterval)
EndEvent

; ─── Action dispatcher ────────────────────────────────────────────────────────

Function ExecuteActionOnNPC(String action, String payload)
    If action == "game_skyrim_attack"
        SynthNPC.StartCombat(Game.GetPlayer())
        
    ElseIf action == "game_skyrim_cast_spell"
        ; Equip the given spell and cast toward the player's target
        String spellName = JDB.solveStr(".SynthRiftVessel.payload_spell")
        ; (spell look-up and equip logic here)
        SynthNPC.SetActorValue("Magicka", SynthNPC.GetActorValue("Magicka") - 50.0)
        
    ElseIf action == "game_skyrim_shout"
        SynthNPC.PlayIdle(IdleShoutStart)  ; requires Idle property
        
    ElseIf action == "game_skyrim_equip"
        String item = JDB.solveStr(".SynthRiftVessel.payload_item")
        ; (item look-up and equip)
        
    ElseIf action == "game_skyrim_use_item"
        ; Find potion in inventory, use it
        SynthNPC.EquipItem(PotionHealth, False, True)
        
    ElseIf action == "game_skyrim_follow"
        ; Set follow package target to player
        (SynthNPC as ActorBase).SetEssential(True)
        Debug.Trace("[SynthRiftVessel] NPC set to follow the player.")
        
    ElseIf action == "game_skyrim_wait"
        SynthNPC.EvaluatePackage()
        Debug.Trace("[SynthRiftVessel] NPC waiting.")
        
    ElseIf action == "game_skyrim_move_to"
        String loc = JDB.solveStr(".SynthRiftVessel.payload_location")
        ; (pathfinding to location)
        Debug.Trace("[SynthRiftVessel] Moving to: " + loc)
        
    EndIf
EndFunction

; ─── Native functions (provided by SKSE plugin) ──────────────────────────────

Function UpdateWorldState() Global Native
Function ExecuteAction(String action, String payload) Global Native
