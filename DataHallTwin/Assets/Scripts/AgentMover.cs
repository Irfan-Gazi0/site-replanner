using System.Collections.Generic;
using UnityEngine;
using TMPro;

namespace DataHall {

// One resource in the twin: its queue state machine and its body in the scene.
// The queue rules are fake_twin's `_finish_current` and `_start_next`, kept
// rule for rule (spec 3.4). TaskBoard calls TwinTick in sorted resource order.
//
// Movement is presentation only. A task's clock starts the moment the queue
// rule lets it start and ends at `start + (end - start)` sim-minutes, whether
// or not the agent has arrived, so the Unity twin and fake_twin agree on every
// time they publish. The trip is short on purpose: 3 m/s crosses the hall in
// about 7 real seconds, well inside the shortest 15-minute task at the default
// 1 sim-minute per second.
public class AgentMover : MonoBehaviour {

    [Tooltip("Must match a resource id in tasks.json: R1, R2, R3 or C1.")]
    public string resourceId = "R1";
    public float metresPerSecond = 3f;
    public TMP_Text label;
    public Color faultColor = Color.red;

    enum MoveState { Idle, Moving, Working }

    readonly List<Assignment> queue = new List<Assignment>();
    readonly List<Vector3> legs = new List<Vector3>();

    MoveState state = MoveState.Idle;
    bool faulted;
    string currentTask = "";
    float endAtSim;
    Vector3 home;
    Renderer bodyRenderer;

    public string CurrentTask { get { return currentTask; } }

    public string Status {
        get {
            if (faulted) return "fault";
            return currentTask != "" ? "busy" : "idle";
        }
    }

    void Start() {
        home = transform.position;
        bodyRenderer = GetComponentInChildren<Renderer>();
        TaskBoard.Instance.Register(this);
        UpdateLabel();
    }

    // --------------------------------------------------------------- queue ---

    public void ClearQueue() {
        queue.Clear();
    }

    public void Enqueue(Assignment a) {
        queue.Add(a);               // TaskBoard.ApplyPlan enqueues in start order
    }

    public string QueueIds() {
        List<string> ids = new List<string>();
        foreach (Assignment a in queue) ids.Add(a.task);
        return string.Join(",", ids);
    }

    // ---------------------------------------------------------------- tick ---

    public void TwinTick() {
        FinishCurrent();
        StartNext();
        Move();
    }

    void FinishCurrent() {
        TaskBoard board = TaskBoard.Instance;
        if (currentTask == "" || board.SimMinutes < endAtSim) return;

        TaskBoard.TaskRuntime task = board.Task(currentTask);
        board.MarkCompleted(task, endAtSim);
        currentTask = "";
        state = MoveState.Idle;
        legs.Clear();
        legs.Add(home);
        UpdateLabel();
    }

    // Start the head of the queue, or nothing: never skip ahead in it.
    void StartNext() {
        TaskBoard board = TaskBoard.Instance;
        if (faulted || currentTask != "" || queue.Count == 0) return;

        Assignment head = queue[0];
        TaskBoard.TaskRuntime task = board.Task(head.task);
        if (task.status == "completed") {
            queue.RemoveAt(0);
            return;
        }
        bool ready = board.PredsCompleted(head.preds);
        if (!ready || board.SimMinutes < head.start) return;

        queue.RemoveAt(0);
        currentTask = head.task;
        endAtSim = board.SimMinutes + (head.end - head.start);
        board.MarkOngoing(task, resourceId, endAtSim);

        PlanRoute(task);
        state = MoveState.Moving;
        UpdateLabel();
    }

    // Transport work picks the load up at the laydown area first (spec P4.2).
    void PlanRoute(TaskBoard.TaskRuntime task) {
        TaskBoard board = TaskBoard.Instance;
        legs.Clear();
        if (task.capability == "transport") legs.Add(board.ZonePoint("LAYDOWN") + Offset());
        legs.Add(board.ZonePoint(task.zone) + Offset());
    }

    // Keeps four agents from standing in the same spot inside a zone.
    Vector3 Offset() {
        switch (resourceId) {
            case "R1": return new Vector3(-1.8f, 0f, -1.2f);
            case "R2": return new Vector3(1.8f, 0f, -1.2f);
            case "R3": return new Vector3(0f, 0f, 1.6f);
            default:   return new Vector3(0f, 0f, -2.6f);
        }
    }

    void Move() {
        if (faulted || legs.Count == 0) return;

        Vector3 target = new Vector3(legs[0].x, transform.position.y, legs[0].z);
        transform.position = Vector3.MoveTowards(
            transform.position, target, metresPerSecond * Time.deltaTime);

        if (Vector3.Distance(transform.position, target) > 0.05f) return;
        legs.RemoveAt(0);
        if (legs.Count == 0 && state == MoveState.Moving) state = MoveState.Working;
    }

    // --------------------------------------------------------------- fault ---

    // The resource stops, turns red and gets status fault. Its ongoing task
    // goes back to pending and its queue is cleared (spec 3.4). The
    // /twin/events message is FaultButton's job.
    public void Fault() {
        faulted = true;
        if (currentTask != "") {
            TaskBoard.Instance.RevertToPending(TaskBoard.Instance.Task(currentTask));
            currentTask = "";
        }
        queue.Clear();
        legs.Clear();
        state = MoveState.Idle;
        if (bodyRenderer != null) bodyRenderer.material.color = faultColor;
        Debug.LogWarning("FAULT " + resourceId + " at t=" + Mathf.Round(TaskBoard.Instance.SimMinutes));
        UpdateLabel();
    }

    void UpdateLabel() {
        if (label == null) return;
        if (faulted) { label.text = resourceId + " · FAULT"; return; }
        label.text = currentTask == "" ? resourceId : resourceId + " · " + currentTask;
    }
}
}
