"""Independent matrix/plane oracles for the directional v5 internal step."""
import numpy as np
import pytest

from oceanroute.bathymetry import BathymetryGrid
from oceanroute.current_dynamics import (current_predictor, stretch_project_blocks,
    bend_project_blocks, project_contact_blocks)


def loading(tangent, velocity, fluid, cable, body):
    relative = fluid-velocity
    normal = relative-np.sum(relative*tangent, axis=1)[:, None]*tangent
    force = cable[:, None]*np.linalg.norm(normal, axis=1)[:, None]*normal
    force += body[:, None]*np.linalg.norm(relative, axis=1)[:, None]*relative
    return {"node_tangent":tangent, "node_current_m_s":fluid,
            "node_total_drag_force_n":force}


def blocks():
    result = np.array([[[.2,.04,.01],[.04,.3,-.02],[.01,-.02,.4]] for _ in range(6)])
    result[[0,-1]] = 0
    return result


@pytest.mark.parametrize("h", [.002,.02,.2])
def test_predictor_matches_independent_dense_directional_momentum_equation(h):
    direction = np.array([-1.,-.7,-.3]); p = np.arange(6)[:, None]*direction
    p[:,2] -= 3
    rest = np.full(5, np.linalg.norm(direction))
    v = np.array([[.04*j,-.02*j,.01*j] for j in range(6)])
    fluid = np.tile([.3,-.2,0.], (6,1))
    tangent = np.tile(direction/np.linalg.norm(direction), (6,1))
    mass = np.array([2.,4.,6.,5.,3.,8.]); cable = np.linspace(4.,9.,6)
    body = np.array([0.,2.,1.,0.,3.,0.]); weight = np.linspace(2.,4.,6)
    local = {"mass":mass,"drag":cable,"body_drag":body,"weight":weight,"ea":np.full(5,1000.)}
    field = loading(tangent,v,fluid,cable,body)
    actual, inverse, axial, normal = current_predictor(p,v,rest,local,field,np.full(6,-100.),np.tile([0.,0.,1.],(6,1)),h)
    expected = np.zeros((6,3))
    for j in range(1,5):
        projector = np.eye(3)-np.outer(tangent[j],tangent[j])
        relative = fluid[j]-v[j]
        resistance = cable[j]*np.linalg.norm(projector@relative)*projector+body[j]*np.linalg.norm(relative)*np.eye(3)
        matrix = mass[j]*np.eye(3)+h*resistance
        rhs = mass[j]*v[j]+h*(resistance@fluid[j]-np.array([0.,0.,weight[j]]))
        expected[j] = np.linalg.solve(matrix,rhs)
        assert inverse[j] == pytest.approx(np.linalg.inv(matrix), abs=1e-14)
        assert np.linalg.eigvalsh(inverse[j]).min() > 0
    assert actual == pytest.approx(expected, abs=1e-12)
    assert np.max(np.abs(axial)) < 1e-12
    assert normal.tolist() == [0.]*6


def test_axial_increment_matches_full_dense_constraint_jacobian_with_cross_direction_mass():
    p = np.array([[0.,0.,-2.],[-1.,-.2,-2.4],[-2.,-.7,-3.],[-3.,-.4,-3.3],[-4.,-.6,-4.],[-5.,-.2,-4.4]])
    length = np.linalg.norm(np.diff(p,axis=0),axis=1)
    rest = .96*length; ea = np.array([1000.,2000.,1500.,4000.,2500.]); h = .02
    inverse = blocks(); multipliers = np.full(5,-.01)
    jacobian = np.zeros((5,18))
    for j in range(5):
        unit = (p[j+1]-p[j])/length[j]
        jacobian[j,3*j:3*j+3] = -unit
        jacobian[j,3*(j+1):3*(j+1)+3] = unit
    metric = np.zeros((18,18))
    for j in range(6):metric[3*j:3*j+3,3*j:3*j+3] = inverse[j]
    compliance = rest/(ea*h*h)
    change = np.linalg.solve(jacobian@metric@jacobian.T+np.diag(compliance),
                            -(length-rest)-compliance*multipliers)
    proposed = np.minimum(multipliers+change,0.)
    expected = p+(metric@jacobian.T@(proposed-multipliers)).reshape(6,3)
    actual = p.copy(); stretch_project_blocks(actual,inverse,rest,ea,h,multipliers)
    assert actual == pytest.approx(expected, abs=1e-13)
    assert multipliers == pytest.approx(proposed, abs=1e-13)


def test_contact_matches_exact_directional_metric_projection_on_oblique_plane():
    x,y = np.array([-10.,0.,10.]),np.array([-10.,0.,10.])
    z = -10.+.2*x[None,:]+.1*y[:,None]
    grid = BathymetryGrid({"schema":"oceanroute.bathymetry.v1","x_m":x.tolist(),"y_m":y.tolist(),"z_m":z.tolist(),
        "source":{"name":"independent oblique plane","horizontal_crs":"LOCAL_CARTESIAN_METRES","origin_projected_m":[0.,0.],"vertical_datum":"model sea zero"}})
    p = np.column_stack((np.linspace(-2.,2.,6),np.zeros(6),np.zeros(6)))
    p[:,2] = -10.+.2*p[:,0]-.2;p[[0,-1],2] += .5
    inverse = blocks(); normal = np.array([-.2,-.1,1.]);normal /= np.linalg.norm(normal)
    expected = p.copy(); expected_lambda = np.zeros(6)
    for j in range(1,5):
        signed = (p[j,2]-(-10.+.2*p[j,0]+.1*p[j,1]))*normal[2]
        expected_lambda[j] = -signed/(normal@inverse[j]@normal)
        expected[j] += expected_lambda[j]*(inverse[j]@normal)
    actual = p.copy(); multipliers = np.zeros(6)
    sweeps,_ = project_contact_blocks(grid,actual,inverse,multipliers)
    assert actual == pytest.approx(expected, abs=1e-12)
    assert multipliers == pytest.approx(expected_lambda, abs=1e-12)
    assert sweeps <= 3
    assert np.min(actual[:,2]-(-10.+.2*actual[:,0]+.1*actual[:,1])) >= -1e-10


def test_future_secant_bending_increment_uses_full_matrix_instead_of_scalar_mass():
    p = np.array([[0.,0.,-2.],[-1.,.1,-3.],[-2.,.4,-4.],[-3.,.2,-5.],[-4.,.1,-6.],[-5.,0.,-7.]])
    rest = np.array([1.2,1.3,1.4,1.5,1.6]);ei = np.array([0.,500.,0.,0.]);h = .02
    inverse = blocks(); multipliers = np.zeros((4,3));j=2
    factors = np.array([1/rest[j-1],-(1/rest[j-1]+1/rest[j]),1/rest[j]])
    constraint = sum(a*p[k] for a,k in zip(factors,[j-1,j,j+1]))
    compliance = (rest[j-1]+rest[j])/(2*ei[j-1]*h*h)
    matrix = sum(a*a*inverse[k] for a,k in zip(factors,[j-1,j,j+1]))+compliance*np.eye(3)
    change = np.linalg.solve(matrix,-constraint)
    expected = p.copy()
    for factor,k in zip(factors,[j-1,j,j+1]):expected[k] += factor*(inverse[k]@change)
    actual = p.copy();bend_project_blocks(actual,inverse,rest,ei,h,multipliers)
    assert actual == pytest.approx(expected, abs=1e-13)
    assert multipliers[j-1] == pytest.approx(change, abs=1e-13)
