# mypy: allow-untyped-defs
# Copyright (c) Meta Platforms, Inc. and affiliates
import logging
from functools import lru_cache
from typing import cast, List, NamedTuple, Tuple

import torch
import torch.distributed._functional_collectives as funcol
import torch.distributed._tensor.api as dtensor
from torch.distributed._tensor.device_mesh import DeviceMesh
from torch.distributed._tensor.placement_types import (
    DTensorSpec,
    Partial,
    Placement,
    Replicate,
    Shard,
    TensorMeta,
)


logger = logging.getLogger(__name__)


class _TransformInfo(NamedTuple):
    mesh_dim: int
    src_dst_placements: Tuple[Placement, Placement]
    # logical_shape on this mesh dimension
    logical_shape: List[int]


@lru_cache(maxsize=None)
def _gen_transform_infos(
    src_spec: DTensorSpec,
    dst_spec: DTensorSpec,
) -> List[_TransformInfo]:
    """
    Generate the transform infos from the source placements to the target placements.

    To transform from source to target placement it might have multiple steps, i.e. it
    might decompose Si -> Sj into Si -> R -> Sj.
    This would detect if there're mis-aligned/nested shardings between src/dst placements.
    E.g. Suppose the redistribution to perform is (Shard(0), Shard(0)) -> (Replicate(), Shard(0)),
    in this case Shard(0) -> Shard(0) for mesh dimension 1 actually needs resharding, because in
    the former is a nested-sharding of a tensor already already sharded dimension 0, whereras
    the latter is the first sharding on tensor dimension 0.
    """
    transform_infos: List[_TransformInfo] = []

    device_mesh = src_spec.device_mesh
    my_coordinate = device_mesh.get_coordinate()
    assert my_coordinate is not None

    # logical shape records the logic tensor shape on the mesh dimension
    # this is useful to ensure uneven sharding gets correct output shape
    initial_logical_shape = list(src_spec.shape)
    if device_mesh.ndim == 1:
        # if device_mesh is 1D, redistribute is a simple direct transformation
        transform_infos.append(
            _TransformInfo(
                mesh_dim=0,
                src_dst_placements=(src_spec.placements[0], dst_spec.placements[0]),
                logical_shape=initial_logical_shape,
            )
        )
        return transform_infos

    # handle multi-dim device mesh placement redistribution
    src_placements = list(src_spec.placements)
    dst_placements = list(dst_spec.placements)
    mesh_dims_to_logical_shape = [initial_logical_shape]

    for i, (src, dst) in enumerate(zip(src_placements, dst_placements)):
        # detect mis-aligned sharding and build logical shapes
        current_logical_shape = mesh_dims_to_logical_shape[i]
        if isinstance(src, Shard):
            if i < device_mesh.ndim - 1:
                # calculate and save the logical shape for this sharding
                mesh_dim_size = device_mesh.size(mesh_dim=i)
                local_shard_size, _ = src._local_shard_size_on_dim(
                    current_logical_shape[src.dim],
                    mesh_dim_size,
                    my_coordinate[i],
                )
                new_logical_shape = list(current_logical_shape)
                new_logical_shape[src.dim] = local_shard_size
                mesh_dims_to_logical_shape.append(new_logical_shape)
        else:
            mesh_dims_to_logical_shape.append(current_logical_shape)

    if src_spec.num_shards > 1:
        # If src_spec have sharding, it could potentially have sharding that is misaligned with dst_spec
        # a common case of this is nested sharding (i.e. (S(0), S(0)) -> (R, S(0))).
        # In those cases, we first traverse from inner placement to outer placement
        # to detect misaligned shardings and properly replicate nested sharding
        search_transform_infos(
            src_placements,
            dst_placements,
            device_mesh.ndim - 1,
            device_mesh.ndim,
            mesh_dims_to_logical_shape,
            transform_infos,
            left_to_right=False,
        )

    # we always traverse from outer placement to inner placement to generate the transform infos
    search_transform_infos(
        src_placements,
        dst_placements,
        0,
        device_mesh.ndim,
        mesh_dims_to_logical_shape,
        transform_infos,
    )

    return transform_infos


def reshardable_from_src_to_dst(
    current_placements, target_placements, mesh_dim
) -> bool:
    # util function to check if we can redistribute from current placements to target placements
    # on the current mesh_dim
    current = current_placements[mesh_dim]
    target = target_placements[mesh_dim]

    # For current that is Shard, check if it's the inner most sharding
    # if not then it's not reshardable, else we check the target sharding
    if current.is_shard():
        for i in reversed(range(len(current_placements))):
            if current_placements[i].is_shard(current.dim):
                if i != mesh_dim:
                    return False
                else:
                    break

    # if target is not shard, we can directly perform redistribute
    if not target.is_shard():
        return True

    shard_dim = target.dim
    # For target is Shard case, check if current/target_placements have the same
    # mesh sharding on the tensor dim BEFORE the current mesh_dim
    current_mesh_sharding, target_mesh_sharding = [], []
    for i, (s, p) in enumerate(zip(current_placements, target_placements)):
        if i >= mesh_dim:
            break
        if s.is_shard(shard_dim):
            current_mesh_sharding.append(i)
        if p.is_shard(shard_dim):
            target_mesh_sharding.append(i)

    return current_mesh_sharding == target_mesh_sharding


def search_transform_infos(
    src_placements: List[Placement],
    dst_placements: List[Placement],
    current_mesh_idx,
    mesh_ndim,
    mesh_dims_to_logical_shape,
    transform_infos: List[_TransformInfo],
    left_to_right=True,
) -> None:
    # short circuit
    if src_placements == dst_placements:
        return

    if current_mesh_idx < 0 or current_mesh_idx >= mesh_ndim:
        return

    src = src_placements[current_mesh_idx]
    dst = dst_placements[current_mesh_idx]

    if reshardable_from_src_to_dst(src_placements, dst_placements, current_mesh_idx):
        # current src_placements can transform directly to dst_placements on mesh_idx
        if src != dst:
            transform_infos.append(
                _TransformInfo(
                    mesh_dim=current_mesh_idx,
                    src_dst_placements=(src, dst),
                    logical_shape=mesh_dims_to_logical_shape[current_mesh_idx],
                )
            )
            src_placements[current_mesh_idx] = dst
        search_transform_infos(
            src_placements,
            dst_placements,
            current_mesh_idx + 1 if left_to_right else current_mesh_idx - 1,
            mesh_ndim,
            mesh_dims_to_logical_shape,
            transform_infos,
            left_to_right,
        )
    elif not left_to_right:
        # if not reshardable, try to unshard this mesh dimension when traversing
        # from inner to outer mesh dimensions (i.e. to clear nested sharding)
        replica = Replicate()
        transform_infos.append(
            _TransformInfo(
                mesh_dim=current_mesh_idx,
                src_dst_placements=(src, replica),
                logical_shape=mesh_dims_to_logical_shape[current_mesh_idx],
            )
        )
        src_placements[current_mesh_idx] = replica
        search_transform_infos(
            src_placements,
            dst_placements,
            current_mesh_idx - 1,
            mesh_ndim,
            mesh_dims_to_logical_shape,
            transform_infos,
            left_to_right,
        )
    else:
        raise RuntimeError(
            f"Could not redistribute from {src_placements} to {dst_placements}!"
        )


def redistribute_local_tensor(
    local_tensor: torch.Tensor,
    current_spec: DTensorSpec,
    target_spec: DTensorSpec,
    *,
    async_op: bool = False,
    is_backward: bool = False,
) -> torch.Tensor:
    """
    This redistribute the local tensor (torch.Tensor) from the current DTensorSpec to
    the target DTensorSpec, which involves the necessary collective calls to transform
    the local shard of the DTensor from its current spec to the target spec.
    """

    if current_spec.mesh != target_spec.mesh:
        # TODO: alltoall/permute reshuffling to change device_mesh if they are not the same
        raise NotImplementedError("Cross device mesh comm not supported yet!")

    new_local_tensor = None
    device_mesh = current_spec.mesh

    my_coordinate = device_mesh.get_coordinate()

    if my_coordinate is None:
        # if rank is not part of mesh, we skip redistribute and simply return local_tensor,
        # which should be an empty tensor
        return local_tensor

    transform_infos = _gen_transform_infos(current_spec, target_spec)

    for transform_info in transform_infos:
        i = transform_info.mesh_dim
        current, target = transform_info.src_dst_placements
        num_chunks = device_mesh.size(mesh_dim=i)

        if current == target:
            # short cut, just use the original local tensor
            new_local_tensor = local_tensor
            continue

        logger.debug("redistribute from %s to %s on mesh dim %s", current, target, i)

        if target.is_replicate():
            # Case 1: target is Replicate
            if current.is_partial():
                partial_spec = cast(Partial, current)
                new_local_tensor = partial_spec._reduce_value(
                    local_tensor, device_mesh, i
                )
            elif current.is_shard():
                current_placement = cast(Shard, current)
                new_local_tensor = current_placement._to_replicate_tensor(
                    local_tensor, device_mesh, i, transform_info.logical_shape
                )
            else:
                raise RuntimeError(
                    f"redistribute from {current} to {target} not supported yet"
                )
        elif target.is_shard():
            # Case 2: target is Shard
            target_placement = cast(Shard, target)
            target_dim = target_placement.dim
            if current.is_partial():
                partial_spec = cast(Partial, current)
                new_local_tensor = partial_spec._reduce_shard_value(
                    local_tensor, device_mesh, i, target_placement
                )
            elif current.is_replicate():
                # split the tensor and return the corresponding cloned local shard
                new_local_tensor = target_placement._replicate_to_shard(
                    local_tensor, device_mesh, i, my_coordinate[i]
                )
            else:
                assert (
                    current.is_shard()
                ), f"Current placement should be shard but found {current}"
                shard_spec = cast(Shard, current)
                if shard_spec.dim != target_placement.dim:
                    new_local_tensor = shard_spec._to_new_shard_dim(
                        local_tensor,
                        device_mesh,
                        i,
                        transform_info.logical_shape,
                        target_placement.dim,
                    )
        elif target.is_partial():
            if current.is_replicate():
                partial_spec = cast(Partial, target)
                # skip the replicate to partial transformation when we are in backward pass
                # In this case we keep the grad as replicate, this is because we don't
                # want to convert the replicated gradients back to partial, although
                # that's logically conform with the same layout, converting the gradients
                # back to partial is actually useless as you would have to do reduce later
                # which would be more expensive than keeping it replicate! For this reason,
                # we keep the replicate grad here.
                new_local_tensor = (
                    partial_spec._partition_value(local_tensor, device_mesh, i)
                    if not is_backward
                    else local_tensor
                )
            elif current.is_shard():
                if not is_backward:
                    raise RuntimeError(
                        f"redistribute from {current} to {target} not supported yet"
                    )
                # for backward shard -> partial, we just need to convert the shard to replicate
                current_placement = cast(Shard, current)
                new_local_tensor = current_placement._to_replicate_tensor(
                    local_tensor, device_mesh, i, transform_info.logical_shape
                )
            else:
                # partial -> partial no op, should never hit
                new_local_tensor = local_tensor

        assert new_local_tensor is not None
        local_tensor = new_local_tensor

    assert new_local_tensor is not None, "redistribute failed!"

    if not async_op and isinstance(new_local_tensor, funcol.AsyncCollectiveTensor):
        new_local_tensor = new_local_tensor.wait()

    return new_local_tensor


class Redistribute(torch.autograd.Function):
    @staticmethod
    def forward(  # type: ignore[override]
        # pyre-fixme[2]: Parameter must be annotated.
        ctx,
        input: "dtensor.DTensor",
        device_mesh: DeviceMesh,
        placements: Tuple[Placement, ...],
        async_op: bool = False,
    ):
        current_spec = input._spec
        ctx.current_spec = current_spec
        ctx.async_op = async_op

        if current_spec.placements != placements:
            target_spec = DTensorSpec(
                device_mesh, placements, tensor_meta=input._spec.tensor_meta
            )

            local_tensor = input._local_tensor
            output = redistribute_local_tensor(
                local_tensor, current_spec, target_spec, async_op=async_op
            )
        else:
            # use the same local tensor if placements are the same.
            output = input._local_tensor
            target_spec = current_spec

        return dtensor.DTensor(
            output,
            target_spec,
            requires_grad=input.requires_grad,
        )

    @staticmethod
    def backward(ctx, grad_output: "dtensor.DTensor"):  # type: ignore[override]
        previous_spec = ctx.current_spec
        current_spec = grad_output._spec
        async_op = ctx.async_op

        local_tensor = grad_output._local_tensor
        output = redistribute_local_tensor(
            local_tensor,
            current_spec,
            previous_spec,
            async_op=async_op,
            is_backward=True,
        )
        # normalize the target placement to replicate if it is partial
        normalized_placements: List[Placement] = []
        for previous_placement in previous_spec.placements:
            if previous_placement.is_partial():
                # keep target placement to replicate instead of partial in this case
                normalized_placements.append(Replicate())
            else:
                normalized_placements.append(previous_placement)

        spec = DTensorSpec(
            previous_spec.device_mesh,
            tuple(normalized_placements),
            tensor_meta=TensorMeta(
                shape=grad_output.shape,
                stride=grad_output.stride(),
                dtype=grad_output.dtype,
            ),
        )
        output_dtensor = dtensor.DTensor(
            output,
            spec,
            requires_grad=grad_output.requires_grad,
        )

        return (
            output_dtensor,
            None,
            None,
            None,
        )
