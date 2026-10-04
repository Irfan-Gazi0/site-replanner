using UnityEngine;
using UnityEngine.UI;

namespace DataHall {

// A "Fault R2" button. Faults the resource, then publishes /twin/events in
// that order, which is the order the bridge relies on (see TwinPublisher).
[RequireComponent(typeof(Button))]
public class FaultButton : MonoBehaviour {

    [Tooltip("Resource to fault: R1, R2, R3 or C1.")]
    public string resourceId = "R2";

    void Start() {
        GetComponent<Button>().onClick.AddListener(Click);
    }

    public void Click() {
        AgentMover mover = TaskBoard.Instance.Mover(resourceId);
        if (mover == null) {
            Debug.LogError("FaultButton: no AgentMover with resourceId " + resourceId);
            return;
        }
        mover.Fault();
        TwinPublisher.Instance.PublishFault(resourceId);
    }
}
}
