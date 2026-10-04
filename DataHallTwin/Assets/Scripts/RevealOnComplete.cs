using UnityEngine;

namespace DataHall {

// Presentation only: shows the hall being built. A rack goes from
// semi-transparent to solid when its zone's rack task completes (T9 or T10),
// and a cable tray appears when its install task completes (T5 or T6).
//
// It only reads task status, so it cannot change what the twin publishes.
public class RevealOnComplete : MonoBehaviour {

    [Tooltip("Task that reveals this object, e.g. T9 for a zone A rack.")]
    public string taskId = "T9";

    [Tooltip("Hide until the task completes, instead of fading in from faint.")]
    public bool hiddenUntilComplete;

    [Range(0f, 1f)] public float startAlpha = 0.25f;

    Renderer[] renderers;
    bool revealed;

    void Start() {
        renderers = GetComponentsInChildren<Renderer>();
        if (hiddenUntilComplete) {
            SetVisible(false);
        } else {
            SetAlpha(startAlpha);
        }
    }

    void Update() {
        if (revealed || TaskBoard.Instance == null) return;
        TaskBoard.TaskRuntime task = TaskBoard.Instance.Task(taskId);
        if (task == null || task.status != "completed") return;

        revealed = true;
        if (hiddenUntilComplete) SetVisible(true);
        SetAlpha(1f);
    }

    void SetVisible(bool visible) {
        foreach (Renderer r in renderers) r.enabled = visible;
    }

    void SetAlpha(float a) {
        foreach (Renderer r in renderers) {
            Color c = r.material.color;
            c.a = a;
            r.material.color = c;
        }
    }
}
}
