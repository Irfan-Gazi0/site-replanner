using System.Collections.Generic;
using System.IO;
using UnityEngine;
using TMPro;

namespace DataHall {

// The twin's world: the sim clock, every task's status, and the plan that is in
// force. It is the C# half of `mvp/ros/fake_twin.py` and must behave the same
// way (spec 3.4). Change both or neither.
//
// Rules, all of them:
//   - the clock advances `simMinutesPerSecond` sim-minutes per real second;
//   - a resource runs its queue strictly in order of `start` and never skips
//     ahead in it;
//   - a new plan replaces every queue, and an ongoing task keeps running;
//   - a fault stops the resource, reverts its ongoing task to pending (a
//     restart from scratch), clears its queue and publishes /twin/events.
//
// The queue state machine lives in AgentMover, one per resource. TaskBoard
// ticks the movers itself, in sorted resource order, so the finish-then-start
// order of fake_twin's `advance` is reproduced exactly instead of depending on
// Unity's Update order.
public class TaskBoard : MonoBehaviour {

    public static TaskBoard Instance { get; private set; }

    [Tooltip("Sim-minutes per real second. 0.5 gives a slower, easier recording.")]
    public float simMinutesPerSecond = 1f;
    public TMP_Text clockLabel;

    public float SimMinutes { get; private set; }
    public int PlanId { get; private set; }

    // One task as the twin tracks it: the 3.3 fields plus the two from
    // tasks.json that the route needs. Duration and preds are not here: both
    // come off the assignment, which is the plan's own copy of them.
    public class TaskRuntime {
        public string id;
        public string status = "pending";   // pending | ongoing | completed
        public string resource = "";        // "" while pending (3.3: no nulls)
        public int start;
        public int end;
        public string zone;
        public string capability;
    }

    readonly Dictionary<string, TaskRuntime> tasks = new Dictionary<string, TaskRuntime>();
    readonly Dictionary<string, Vector3> zonePoints = new Dictionary<string, Vector3>();
    readonly Dictionary<string, ResourceSpec> resourceSpecs = new Dictionary<string, ResourceSpec>();
    readonly List<AgentMover> movers = new List<AgentMover>();

    void Awake() {
        Instance = this;
        LoadTasksFile();
    }

    void LoadTasksFile() {
        string path = Path.Combine(Application.streamingAssetsPath, "tasks.json");
        if (!File.Exists(path)) {
            Debug.LogError("TaskBoard: no tasks.json at " + path + " (run `make sync-unity`)");
            return;
        }
        TasksDoc doc = JsonUtility.FromJson<TasksDoc>(File.ReadAllText(path));

        foreach (ZoneSpec z in doc.zones) {
            zonePoints[z.id] = new Vector3(z.x, 0f, z.z);
        }
        foreach (ResourceSpec r in doc.resources) {
            resourceSpecs[r.id] = r;
        }
        foreach (TaskSpec t in doc.tasks) {
            tasks[t.id] = new TaskRuntime {
                id = t.id,
                zone = t.zone,
                capability = t.capability,
            };
        }
        Debug.Log("TaskBoard: loaded " + tasks.Count + " tasks, " + resourceSpecs.Count + " resources");
    }

    // ------------------------------------------------------------- lookups ---

    public TaskRuntime Task(string id) {
        TaskRuntime t;
        return tasks.TryGetValue(id, out t) ? t : null;
    }

    public ResourceSpec Resource(string id) {
        ResourceSpec r;
        return resourceSpecs.TryGetValue(id, out r) ? r : null;
    }

    public Vector3 ZonePoint(string zoneId) {
        Vector3 p;
        return zonePoints.TryGetValue(zoneId, out p) ? p : Vector3.zero;
    }

    public bool PredsCompleted(string[] preds) {
        if (preds == null) return true;
        foreach (string p in preds) {
            TaskRuntime t = Task(p);
            if (t == null || t.status != "completed") return false;
        }
        return true;
    }

    public void Register(AgentMover mover) {
        if (!movers.Contains(mover)) movers.Add(mover);
        movers.Sort((a, b) => string.CompareOrdinal(a.resourceId, b.resourceId));
    }

    public AgentMover Mover(string resourceId) {
        foreach (AgentMover m in movers) {
            if (m.resourceId == resourceId) return m;
        }
        return null;
    }

    // ---------------------------------------------------------------- plan ---

    // A new plan replaces every queue (3.4). Completed tasks are skipped, and
    // an ongoing task keeps its actual times and is not re-queued: the solver
    // freezes ongoing tasks, so the new plan has it on the same resource.
    public void ApplyPlan(PlanMsg plan) {
        PlanId = plan.plan_id;
        foreach (AgentMover m in movers) m.ClearQueue();

        List<Assignment> ordered = new List<Assignment>(plan.assignments ?? new Assignment[0]);
        ordered.Sort((a, b) => a.start != b.start
            ? a.start.CompareTo(b.start)
            : string.CompareOrdinal(a.task, b.task));

        foreach (Assignment a in ordered) {
            TaskRuntime task = Task(a.task);
            AgentMover mover = Mover(a.resource);
            if (task == null || mover == null || task.status == "completed") continue;
            if (task.status == "ongoing") continue;

            task.resource = a.resource;
            task.start = a.start;
            task.end = a.end;
            mover.Enqueue(a);
        }
        Debug.Log("plan " + PlanId + " applied at t=" + Mathf.Round(SimMinutes) + ": " + QueueSummary());
    }

    string QueueSummary() {
        List<string> parts = new List<string>();
        foreach (AgentMover m in movers) parts.Add(m.resourceId + "=[" + m.QueueIds() + "]");
        return string.Join(", ", parts);
    }

    // --------------------------------------------------------- transitions ---

    public void MarkOngoing(TaskRuntime task, string resourceId, float endAtSim) {
        task.status = "ongoing";
        task.resource = resourceId;
        task.start = Mathf.RoundToInt(SimMinutes);
        task.end = Mathf.RoundToInt(endAtSim);
    }

    public void MarkCompleted(TaskRuntime task, float endAtSim) {
        task.status = "completed";
        task.end = Mathf.RoundToInt(endAtSim);
    }

    // A fault is a restart from scratch, not a resume (3.4).
    public void RevertToPending(TaskRuntime task) {
        task.status = "pending";
        task.resource = "";
        task.start = 0;
        task.end = 0;
    }

    // ---------------------------------------------------------------- tick ---

    void Update() {
        SimMinutes += Time.deltaTime * simMinutesPerSecond;
        // Finish, then start, one resource at a time, in sorted id order.
        foreach (AgentMover m in movers) m.TwinTick();
        if (clockLabel != null) clockLabel.text = "t = " + Mathf.FloorToInt(SimMinutes) + " min";
    }

    // --------------------------------------------------------------- state ---

    public TwinState BuildState() {
        List<TaskState> ts = new List<TaskState>();
        foreach (TaskRuntime t in tasks.Values) {
            ts.Add(new TaskState {
                id = t.id, status = t.status, resource = t.resource,
                start = t.start, end = t.end,
            });
        }
        List<ResourceState> rs = new List<ResourceState>();
        foreach (AgentMover m in movers) {
            rs.Add(new ResourceState { id = m.resourceId, status = m.Status, task = m.CurrentTask });
        }
        return new TwinState {
            t = Mathf.Round(SimMinutes * 10f) / 10f,
            tasks = ts.ToArray(),
            resources = rs.ToArray(),
        };
    }
}
}
